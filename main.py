import glob
import os
import subprocess
import ffmpeg
from deep_translator import GoogleTranslator
import pysrt
from pydub import AudioSegment
import asyncio
import threading
import tkinter as tk
from tkinter import ttk, filedialog
from tkinter.scrolledtext import ScrolledText
from gtts import gTTS
import datetime

"""
Script para processamento de vídeo com TTS (Text-to-Speech) em lotes.
Este programa realiza as seguintes operações:
  1. Seleciona um vídeo (.mp4) e seu arquivo de legendas (.srt) de um diretório.
  2. Lê e processa as legendas, traduzindo e gerando áudio para cada trecho via Google TTS.
  3. Ajusta os segmentos de áudio para sincronizá-los com o vídeo.
  4. Combina os segmentos de áudio e substitui o áudio original do vídeo.
  5. Exibe uma interface gráfica com tkinter para facilitar a utilização.
"""

def get_video_duration_ms(video_path):
    """
    Retorna a duração do vídeo em milissegundos utilizando o ffprobe.

    Args:
        video_path (str): Caminho para o arquivo de vídeo.

    Returns:
        int: Duração do vídeo em milissegundos.
    """
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
            video_path
        ],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    duration_sec = float(result.stdout)
    return int(duration_sec * 1000)

def read_srt_subtitles(file_path):
    """
    Lê um arquivo de legendas no formato SRT.

    Args:
        file_path (str): Caminho para o arquivo .srt.

    Returns:
        pysrt.SubRipFile: Objeto contendo as legendas.
    """
    return pysrt.open(file_path, encoding="utf-8")

async def async_generate_tts_segment(text, output_audio, duration_ms=None, log_callback=print):
    """
    Gera um segmento de áudio a partir de um texto utilizando o Google TTS de forma assíncrona.
    Em caso de erro, gera um áudio silencioso com a duração especificada.

    Args:
        text (str): Texto a ser convertido em áudio.
        output_audio (str): Caminho para salvar o arquivo de áudio.
        duration_ms (int, opcional): Duração desejada do áudio em milissegundos.
        log_callback (function): Função para log de mensagens.

    Returns:
        None
    """
    log_callback(f"Gerando TTS para: {text[:30]}... (Google TTS)", "INFO", "tts_gerando")
    try:
        loop = asyncio.get_running_loop()
        # Executa a geração do áudio em um executor para não bloquear o loop de eventos
        await loop.run_in_executor(None, lambda: gTTS(text, lang="pt").save(output_audio))
        log_callback(f"Trecho salvo em: {output_audio}", "INFO", "tts_sucesso")
        return
    except Exception as e:
        log_callback(f"Erro no Google TTS: {e}", "ERROR", "tts_gerando")
    # Em caso de erro, gera um áudio silencioso com a duração especificada ou padrão de 1 segundo
    if duration_ms is not None and duration_ms > 0:
        log_callback(f"Gerando áudio silencioso para {duration_ms} ms", "INFO", "tts_gerando")
        silent_audio = AudioSegment.silent(duration=duration_ms)
        silent_audio.export(output_audio, format="wav")
    else:
        silent_audio = AudioSegment.silent(duration=1000)
        silent_audio.export(output_audio, format="wav")
    log_callback(f"Trecho salvo (silencioso) em: {output_audio}", "INFO", "tts_sucesso")

async def process_subtitles_batch(subtitles, batch_size, source_language, target_language, temp_audio_dir, log_callback, progress_callback, cancel_flag):
    """
    Processa as legendas em lotes de forma assíncrona, realizando:
      - Tradução do texto do idioma de origem para o idioma de destino.
      - Geração de áudio TTS do texto traduzido.
      - Criação de um segmento de áudio com o tempo correspondente da legenda.

    Args:
        subtitles (pysrt.SubRipFile): Legendas lidas do arquivo SRT.
        batch_size (int): Número máximo de tarefas simultâneas.
        source_language (str): Código do idioma de origem.
        target_language (str): Código do idioma de destino.
        temp_audio_dir (str): Diretório para salvar temporariamente os segmentos de áudio.
        log_callback (function): Função para log de mensagens.
        progress_callback (function): Função para atualização da barra de progresso.
        cancel_flag (function): Função que retorna True se o usuário cancelou o processamento.

    Returns:
        list: Lista de dicionários com informações de cada segmento (início, duração e caminho do arquivo).
    """
    translator = GoogleTranslator(source=source_language, target=target_language)
    total = len(subtitles)
    segments = []
    sem = asyncio.Semaphore(batch_size)

    async def process_subtitle(i, sub):
        # Verifica se o processamento foi cancelado antes de iniciar a tradução
        if cancel_flag():
            raise asyncio.CancelledError("Processamento cancelado pelo usuário antes da tradução")
        async with sem:
            texto_original = sub.text.replace(">>", "").strip()
            if not texto_original:
                progress_callback(i + 1, total)
                return None
            # Realiza a tradução do texto de forma assíncrona
            loop = asyncio.get_running_loop()
            texto_traduzido = await loop.run_in_executor(None, translator.translate, texto_original)
            # Verifica se houve cancelamento após a tradução
            if cancel_flag():
                raise asyncio.CancelledError("Processamento cancelado pelo usuário após tradução")
            # Calcula o tempo de início e fim do segmento (em milissegundos)
            start_ms = (sub.start.hours * 3600000 +
                        sub.start.minutes * 60000 +
                        sub.start.seconds * 1000 +
                        sub.start.milliseconds)
            end_ms = (sub.end.hours * 3600000 +
                      sub.end.minutes * 60000 +
                      sub.end.seconds * 1000 +
                      sub.end.milliseconds)
            duration_ms = end_ms - start_ms
            # Define o caminho para salvar o segmento de áudio
            segment_audio_path = os.path.join(temp_audio_dir, f"segment_{i}.wav")
            await async_generate_tts_segment(texto_traduzido, segment_audio_path, duration_ms, log_callback)
            progress_callback(i + 1, total)
            return {"start": start_ms, "duration": duration_ms, "file": segment_audio_path}

    tasks = [process_subtitle(i, sub) for i, sub in enumerate(subtitles)]
    try:
        results = await asyncio.gather(*tasks)
    except asyncio.CancelledError as e:
        log_callback("Processamento cancelado pelo usuário.", "ERROR", "geral")
        raise e

    for r in results:
        if r is not None:
            segments.append(r)
    return segments

def change_audio_speed_ffmpeg(input_file, output_file, factor):
    """
    Ajusta a velocidade do áudio utilizando o filtro 'atempo' do ffmpeg.

    Args:
        input_file (str): Caminho para o arquivo de áudio de entrada.
        output_file (str): Caminho para salvar o áudio ajustado.
        factor (float): Fator de ajuste de velocidade (maior que 1 aumenta a duração, menor diminui).

    Returns:
        None
    """
    filters = []
    remaining = factor
    # Divide o fator em múltiplos se for maior que 2, para compor os filtros necessários
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    filters.append(f"atempo={remaining}")
    filter_str = ",".join(filters)
    command = ["ffmpeg", "-y", "-i", input_file, "-filter:a", filter_str, output_file]
    subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def combine_audio_segments_gui(segments, output_audio, total_duration_ms, log_callback, progress_callback):
    """
    Combina os segmentos de áudio ajustando cada trecho para a duração correta e sincronizando com o vídeo.
    Preenche eventuais lacunas com áudio silencioso e salva o áudio final.

    Args:
        segments (list): Lista de dicionários contendo informações de cada segmento.
        output_audio (str): Caminho para salvar o áudio combinado.
        total_duration_ms (int): Duração total do vídeo em milissegundos.
        log_callback (function): Função para log de mensagens.
        progress_callback (function): Função para atualização da barra de progresso.

    Returns:
        None
    """
    log_callback("Combinando segmentos de áudio...", "INFO", "geral")
    final_audio = AudioSegment.silent(duration=0)
    current_time = 0
    segments.sort(key=lambda x: x["start"])
    total = len(segments)
    for idx, seg in enumerate(segments):
        progress_callback(idx + 1, total)
        scheduled_start = seg["start"]
        expected_duration = seg["duration"]
        # Se houver intervalo entre segmentos, adiciona silêncio para sincronização
        if scheduled_start > current_time:
            gap = scheduled_start - current_time
            final_audio += AudioSegment.silent(duration=gap)
            current_time = scheduled_start
        try:
            # Carrega o segmento de áudio detectando o formato automaticamente
            seg_audio = AudioSegment.from_file(seg["file"])
        except Exception as e:
            log_callback(f"Erro ao ler {seg['file']}: {e}. Pulando este segmento.", "ERROR", "geral")
            continue
        actual_duration = len(seg_audio)
        # Ajusta o segmento para que a duração corresponda à esperada
        if actual_duration > expected_duration:
            factor = actual_duration / expected_duration
            adjusted_file = os.path.join(os.path.dirname(seg["file"]), f"adjusted_{idx}.wav")
            change_audio_speed_ffmpeg(seg["file"], adjusted_file, factor)
            seg_audio = AudioSegment.from_file(adjusted_file)
            if len(seg_audio) > expected_duration:
                seg_audio = seg_audio[:expected_duration]
            elif len(seg_audio) < expected_duration:
                seg_audio += AudioSegment.silent(duration=expected_duration - len(seg_audio))
        elif actual_duration < expected_duration:
            seg_audio += AudioSegment.silent(duration=expected_duration - actual_duration)
        final_audio += seg_audio
        current_time += expected_duration
    # Garante que o áudio final tenha a duração total do vídeo
    if len(final_audio) < total_duration_ms:
        final_audio += AudioSegment.silent(duration=total_duration_ms - len(final_audio))
    final_audio = final_audio[:total_duration_ms]
    final_audio.export(output_audio, format="wav")
    log_callback(f"Áudio combinado salvo em: {output_audio}", "INFO", "geral")

def replace_audio(video_path, new_audio_path, output_video_path, base_path, log_callback):
    """
    Remove o áudio original do vídeo e insere o novo áudio sincronizado.

    Args:
        video_path (str): Caminho para o vídeo original.
        new_audio_path (str): Caminho para o novo áudio a ser inserido.
        output_video_path (str): Caminho para salvar o vídeo final.
        base_path (str): Diretório base para arquivos temporários.
        log_callback (function): Função para log de mensagens.

    Returns:
        None
    """
    log_callback("Removendo áudio original do vídeo...", "INFO", "geral")
    silent_video = os.path.join(base_path, "silent.mp4")
    # Cria um vídeo sem áudio
    ffmpeg.input(video_path).output(silent_video, vcodec="copy", an=None, threads=4).run(overwrite_output=True)
    
    log_callback("Adicionando novo áudio ao vídeo...", "INFO", "geral")
    # Combina o vídeo silencioso com o novo áudio, mantendo a cópia do vídeo
    ffmpeg.output(
        ffmpeg.input(silent_video).video,
        ffmpeg.input(new_audio_path).audio,
        output_video_path,
        vcodec="copy", acodec="aac", threads=4
    ).run(overwrite_output=True)
    
    log_callback(f"Vídeo final gerado em: {output_video_path}", "INFO", "geral")

class Application(tk.Tk):
    """
    Classe que representa a interface gráfica da aplicação.
    Responsável por criar a janela, gerenciar os widgets e controlar o fluxo de processamento.
    """
    def __init__(self):
        super().__init__()
        self.title("Processamento de Vídeo com TTS em Lotes (Google TTS)")
        self.geometry("750x600")
        self.create_widgets()
        self.base_path = None
        self.cancel_requested = False  # Flag para controle de cancelamento

    def create_widgets(self):
        """
        Cria e organiza os widgets da interface, incluindo:
          - Seleção do diretório base.
          - Configurações de batch e idiomas.
          - Botões de iniciar e cancelar processamento.
          - Barras de progresso e área de logs.
        """
        # Frame para seleção do diretório base
        frame_dir = ttk.Frame(self)
        frame_dir.pack(pady=10, padx=10, fill='x')
        lbl_dir = ttk.Label(frame_dir, text="Diretório Base:")
        lbl_dir.pack(side='left')
        self.dir_var = tk.StringVar()
        self.entry_dir = ttk.Entry(frame_dir, textvariable=self.dir_var, width=50)
        self.entry_dir.pack(side='left', padx=5)
        btn_browse = ttk.Button(frame_dir, text="Selecionar", command=self.select_directory)
        btn_browse.pack(side='left')
        
        # Frame para configurações de TTS, batch e idiomas
        frame_info = ttk.Frame(self)
        frame_info.pack(pady=5, padx=10, fill='x')
        lbl_info = ttk.Label(frame_info, text="Método TTS: Google (usado automaticamente)")
        lbl_info.pack(side='left')
        
        lbl_batch = ttk.Label(frame_info, text="Batch Size:")
        lbl_batch.pack(side='left', padx=5)
        self.batch_size_var = tk.IntVar(value=5)
        spin_batch = ttk.Spinbox(frame_info, from_=1, to=100, textvariable=self.batch_size_var, width=5)
        spin_batch.pack(side='left', padx=5)
        
        lbl_source = ttk.Label(frame_info, text="Idioma da Legenda:")
        lbl_source.pack(side='left', padx=5)
        self.source_language = tk.StringVar(value="auto")
        combo_source = ttk.Combobox(frame_info, textvariable=self.source_language, values=["auto", "pt", "en", "es", "fr", "de"], state="readonly", width=5)
        combo_source.pack(side='left', padx=5)
        
        lbl_target = ttk.Label(frame_info, text="Idioma de Tradução:")
        lbl_target.pack(side='left', padx=5)
        self.target_language = tk.StringVar(value="pt")
        combo_target = ttk.Combobox(frame_info, textvariable=self.target_language, values=["pt", "en", "es", "fr", "de"], state="readonly", width=5)
        combo_target.pack(side='left', padx=5)
        
        # Frame com botões para iniciar e cancelar o processamento
        frame_buttons = ttk.Frame(self)
        frame_buttons.pack(pady=10)
        self.btn_start = ttk.Button(frame_buttons, text="Iniciar Processamento", command=self.start_processing)
        self.btn_start.pack(side='left', padx=5)
        self.btn_cancel = ttk.Button(frame_buttons, text="Cancelar", command=self.cancel_processing, state="disabled")
        self.btn_cancel.pack(side='left', padx=5)
        
        # Frame para as barras de progresso
        frame_progress = ttk.Frame(self)
        frame_progress.pack(pady=5, padx=10, fill='x')
        lbl_subtitles = ttk.Label(frame_progress, text="Processamento das Legendas (TTS em lotes):")
        lbl_subtitles.pack(anchor='w')
        self.progress_subtitles = ttk.Progressbar(frame_progress, orient='horizontal', mode='determinate')
        self.progress_subtitles.pack(fill='x', pady=2)
        
        lbl_audio = ttk.Label(frame_progress, text="Combinação de Áudio:")
        lbl_audio.pack(anchor='w')
        self.progress_audio = ttk.Progressbar(frame_progress, orient='horizontal', mode='determinate')
        self.progress_audio.pack(fill='x', pady=2)
        
        # Notebook para exibir os logs de diferentes categorias
        self.log_notebook = ttk.Notebook(self)
        self.log_notebook.pack(pady=10, padx=10, fill='both', expand=True)
        
        self.frame_tts_gerando = ttk.Frame(self.log_notebook)
        self.frame_tts_sucesso = ttk.Frame(self.log_notebook)
        self.frame_geral = ttk.Frame(self.log_notebook)
        
        self.log_notebook.add(self.frame_tts_gerando, text="Geração TTS")
        self.log_notebook.add(self.frame_tts_sucesso, text="Sucesso TTS")
        self.log_notebook.add(self.frame_geral, text="Geral")
        
        self.log_tts_gerando = ScrolledText(self.frame_tts_gerando, height=8, font=("Courier", 10))
        self.log_tts_gerando.pack(fill='both', expand=True)
        self.log_tts_gerando.tag_config("INFO", foreground="black")
        self.log_tts_gerando.tag_config("ERROR", foreground="red")
        
        self.log_tts_sucesso = ScrolledText(self.frame_tts_sucesso, height=8, font=("Courier", 10))
        self.log_tts_sucesso.pack(fill='both', expand=True)
        self.log_tts_sucesso.tag_config("INFO", foreground="black")
        self.log_tts_sucesso.tag_config("ERROR", foreground="red")
        
        self.log_geral = ScrolledText(self.frame_geral, height=8, font=("Courier", 10))
        self.log_geral.pack(fill='both', expand=True)
        self.log_geral.tag_config("INFO", foreground="black")
        self.log_geral.tag_config("ERROR", foreground="red")

    def select_directory(self):
        """
        Abre uma janela para seleção do diretório base e atualiza a interface com o caminho escolhido.
        """
        directory = filedialog.askdirectory()
        if directory:
            self.base_path = directory
            self.dir_var.set(directory)
            self.log_message(f"Diretório selecionado: {directory}", "INFO", "geral")
    
    def log_message(self, message, level="INFO", category="geral"):
        """
        Registra uma mensagem com timestamp em um dos campos de log da interface.

        Args:
            message (str): Mensagem a ser registrada.
            level (str): Nível da mensagem (INFO ou ERROR).
            category (str): Categoria do log (geral, tts_gerando ou tts_sucesso).
        """
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        formatted_message = f"[{timestamp}] {message}\n"
        if category == "tts_gerando":
            self.log_tts_gerando.insert(tk.END, formatted_message, level)
            self.log_tts_gerando.see(tk.END)
        elif category == "tts_sucesso":
            self.log_tts_sucesso.insert(tk.END, formatted_message, level)
            self.log_tts_sucesso.see(tk.END)
        else:
            self.log_geral.insert(tk.END, formatted_message, level)
            self.log_geral.see(tk.END)
    
    def update_progress_subtitles(self, value, maximum):
        """
        Atualiza a barra de progresso do processamento das legendas.

        Args:
            value (int): Valor atual do progresso.
            maximum (int): Valor máximo da barra de progresso.
        """
        self.progress_subtitles['maximum'] = maximum
        self.progress_subtitles['value'] = value
        self.progress_subtitles.update_idletasks()
    
    def update_progress_audio(self, value, maximum):
        """
        Atualiza a barra de progresso da combinação de áudio.

        Args:
            value (int): Valor atual do progresso.
            maximum (int): Valor máximo da barra de progresso.
        """
        self.progress_audio['maximum'] = maximum
        self.progress_audio['value'] = value
        self.progress_audio.update_idletasks()
    
    def cancel_processing(self):
        """
        Define a flag de cancelamento e registra a ação de cancelamento.
        """
        self.cancel_requested = True
        self.log_message("Cancelando o processamento...", "INFO", "geral")
    
    def start_processing(self):
        """
        Verifica se o diretório base foi selecionado e inicia o processamento em uma thread separada.
        """
        if not self.base_path:
            self.log_message("Por favor, selecione um diretório base primeiro.", "ERROR", "geral")
            return
        self.cancel_requested = False
        self.btn_start.config(state="disabled")
        self.btn_cancel.config(state="normal")
        thread = threading.Thread(target=self.run_process)
        thread.start()
    
    def run_process(self):
        """
        Função principal que realiza as seguintes etapas:
          1. Seleciona o arquivo de vídeo (.mp4) e de legendas (.srt) do diretório base.
          2. Lê e processa as legendas utilizando TTS e tradução.
          3. Combina os segmentos de áudio para sincronização com o vídeo.
          4. Substitui o áudio original do vídeo pelo novo áudio gerado.
        """
        base_path = self.base_path
        video_files = glob.glob(os.path.join(base_path, "*.mp4"))
        if not video_files:
            self.log_message("Nenhum arquivo .mp4 encontrado no diretório.", "ERROR", "geral")
            self.btn_start.config(state="normal")
            self.btn_cancel.config(state="disabled")
            return
        video_file = video_files[0]
        self.log_message(f"Arquivo de vídeo selecionado: {video_file}", "INFO", "geral")
        
        srt_files = glob.glob(os.path.join(base_path, "*.srt"))
        if not srt_files:
            self.log_message("Nenhum arquivo .srt encontrado no diretório.", "ERROR", "geral")
            self.btn_start.config(state="normal")
            self.btn_cancel.config(state="disabled")
            return
        subtitle_file = srt_files[0]
        self.log_message(f"Arquivo de legendas selecionado: {subtitle_file}", "INFO", "geral")
        
        output_audio_file = os.path.join(base_path, "audio_pt_sync.wav")
        output_video = os.path.join(base_path, "video_with_translated_audio.mp4")
        temp_audio_dir = os.path.join(base_path, "temp_audio")
        os.makedirs(temp_audio_dir, exist_ok=True)
        
        self.log_message("Lendo legendas...", "INFO", "geral")
        subtitles = read_srt_subtitles(subtitle_file)
        
        self.log_message("Processando legendas (TTS em lotes)...", "INFO", "geral")
        try:
            segments = asyncio.run(
                process_subtitles_batch(
                    subtitles,
                    batch_size=self.batch_size_var.get(),
                    source_language=self.source_language.get(),
                    target_language=self.target_language.get(),
                    temp_audio_dir=temp_audio_dir,
                    log_callback=self.log_message,
                    progress_callback=self.update_progress_subtitles,
                    cancel_flag=lambda: self.cancel_requested
                )
            )
        except asyncio.CancelledError:
            self.log_message("Processamento interrompido pelo usuário.", "ERROR", "geral")
            self.btn_start.config(state="normal")
            self.btn_cancel.config(state="disabled")
            return
        except Exception as e:
            self.log_message(f"Erro no processamento das legendas: {e}", "ERROR", "geral")
            self.btn_start.config(state="normal")
            self.btn_cancel.config(state="disabled")
            return
        
        if self.cancel_requested:
            self.log_message("Processamento cancelado. Abortando as etapas seguintes.", "ERROR", "geral")
            self.btn_start.config(state="normal")
            self.btn_cancel.config(state="disabled")
            return
        
        self.log_message("Obtendo duração total do vídeo...", "INFO", "geral")
        total_duration_ms = get_video_duration_ms(video_file)
        
        self.log_message("Combinando segmentos de áudio...", "INFO", "geral")
        combine_audio_segments_gui(
            segments,
            output_audio_file,
            total_duration_ms,
            log_callback=self.log_message,
            progress_callback=self.update_progress_audio
        )
        
        self.log_message("Substituindo áudio original do vídeo...", "INFO", "geral")
        replace_audio(video_file, output_audio_file, output_video, base_path, self.log_message)
        
        self.log_message(f"Processo concluído! O vídeo final está em: {output_video}", "INFO", "geral")
        self.btn_start.config(state="normal")
        self.btn_cancel.config(state="disabled")

if __name__ == "__main__":
    # Inicializa e executa a interface gráfica
    app = Application()
    app.mainloop()
