import os
import warnings
from typing import List, Union

import ctranslate2
import faster_whisper
import numpy as np
import torch
from transformers import Pipeline
from transformers.pipelines.pt_utils import PipelineIterator

from .audio import N_SAMPLES, SAMPLE_RATE, load_audio, log_mel_spectrogram
from .vad import load_vad_model, merge_chunks
from .types import TranscriptionResult, SingleSegment

def find_numeral_symbol_tokens(tokenizer):
    numeral_symbol_tokens = []
    for i in range(tokenizer.eot):
        token = tokenizer.decode([i]).removeprefix(" ")
        has_numeral_symbol = any(c in "0123456789%$£" for c in token)
        if has_numeral_symbol:
            numeral_symbol_tokens.append(i)
    return numeral_symbol_tokens

def load_model(whisper_arch,
               device,
               device_index=0,
               compute_type="float16",
               asr_options=None,
               language=None,
               vad_options=None,
               model=None,
               task="transcribe",
               download_root=None):
    '''Load a Whisper model for inference.
    Args:
        whisper_arch: str - The name of the Whisper model to load.
        device: str - The device to load the model on.
        compute_type: str - The compute type to use for the model.
        options: dict - A dictionary of options to use for the model.
        language: str - The language of the model. (use English for now)
        download_root: Optional[str] - The root directory to download the model to.
    Returns:
        A Whisper pipeline.
    '''

    if whisper_arch.endswith(".en"):
        language = "en"

    model = WhisperModel(whisper_arch,
                         device=device,
                         device_index=device_index,
                         compute_type=compute_type,
                         download_root=download_root)
    if language is not None:
        tokenizer = faster_whisper.tokenizer.Tokenizer(model.hf_tokenizer, model.model.is_multilingual, task=task, language=language)
    else:
        print("No language specified, language will be first be detected for each audio file (increases inference time).")
        tokenizer = None

    default_asr_options =  {
        "beam_size": 5,
        "best_of": 5,
        "patience": 1,
        "length_penalty": 1,
        "temperatures": [0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
        "compression_ratio_threshold": 2.4,
        "log_prob_threshold": -1.0,
        "no_speech_threshold": 0.6,
        "condition_on_previous_text": False,
        "initial_prompt": None,
        "prefix": None,
        "suppress_blank": True,
        "suppress_tokens": [-1],
        "without_timestamps": True,
        "max_initial_timestamp": 0.0,
        "word_timestamps": False,
        "prepend_punctuations": "\"'“¿([{-",
        "append_punctuations": "\"'.。,，!！?？:：”)]}、",
        "suppress_numerals": False,
    }

    if asr_options is not None:
        default_asr_options.update(asr_options)

    if default_asr_options["suppress_numerals"]:
        if tokenizer is None:
            tokenizer = faster_whisper.tokenizer.Tokenizer(model.hf_tokenizer, model.model.is_multilingual, task=task, language="en")
        numeral_symbol_tokens = find_numeral_symbol_tokens(tokenizer)
        print(f"Suppressing numeral and symbol tokens: {numeral_symbol_tokens}")
        default_asr_options["suppress_tokens"] += numeral_symbol_tokens
        default_asr_options["suppress_tokens"] = list(set(default_asr_options["suppress_tokens"]))
    del default_asr_options["suppress_numerals"]

    default_asr_options = faster_whisper.transcribe.TranscriptionOptions(**default_asr_options)

    default_vad_options = {
        "vad_onset": 0.500,
        "vad_offset": 0.363
    }

    if vad_options is not None:
        default_vad_options.update(vad_options)

    vad_model = load_vad_model(torch.device(device), use_auth_token=None, **default_vad_options)

    return FasterWhisperPipeline(model, vad_model, default_asr_options, tokenizer)



class WhisperModel(faster_whisper.WhisperModel):
    '''
    FasterWhisperModel provides batched inference for faster-whisper.
    Currently only works in non-timestamp mode and fixed prompt for all samples in batch.
    '''

    def generate_segment_batched(self, features: np.ndarray, tokenizer: faster_whisper.tokenizer.Tokenizer, options: faster_whisper.transcribe.TranscriptionOptions, encoder_output = None):
        batch_size = features.shape[0]
        all_tokens = []
        prompt_reset_since = 0
        if options.initial_prompt is not None:
            initial_prompt = " " + options.initial_prompt.strip()
            initial_prompt_tokens = tokenizer.encode(initial_prompt)
            all_tokens.extend(initial_prompt_tokens)
        previous_tokens = all_tokens[prompt_reset_since:]
        prompt = self.get_prompt(
            tokenizer,
            previous_tokens,
            without_timestamps=options.without_timestamps,
            prefix=options.prefix,
        )

        encoder_output = self.encode(features)

        max_initial_timestamp_index = int(
            round(options.max_initial_timestamp / self.time_precision)
        )

        result = self.model.generate(
                encoder_output,
                [prompt] * batch_size,
                length_penalty=options.length_penalty,
                max_length=self.max_length,
                suppress_blank=options.suppress_blank,
                suppress_tokens=options.suppress_tokens,
            )

        tokens_batch = [x.sequences_ids[0] for x in result]

        def decode_batch(tokens: List[List[int]]) -> str:
            res = []
            for tk in tokens:
                res.append([token for token in tk if token < tokenizer.eot])
            # text_tokens = [token for token in tokens if token < self.eot]
            return tokenizer.tokenizer.decode_batch(res)

        text = decode_batch(tokens_batch)

        return text

    def encode(self, features: np.ndarray) -> ctranslate2.StorageView:
        # When the model is running on multiple GPUs, the encoder output should be moved
        # to the CPU since we don't know which GPU will handle the next job.
        to_cpu = self.model.device == "cuda" and len(self.model.device_index) > 1
        # unsqueeze if batch size = 1
        if len(features.shape) == 2:
            features = np.expand_dims(features, 0)
        features = faster_whisper.transcribe.get_ctranslate2_storage(features)

        return self.model.encode(features, to_cpu=to_cpu)

class FasterWhisperPipeline(Pipeline):
    """
    Huggingface Pipeline wrapper for FasterWhisperModel.
    """
    # TODO:
    # - add support for timestamp mode
    # - add support for custom inference kwargs

    def __init__(
            self,
            model,
            vad,
            options,
            tokenizer=None,
            device: Union[int, str, "torch.device"] = -1,
            framework = "pt",
            merge_threshold=None, #added
            **kwargs
    ):
        self.model = model
        self.tokenizer = tokenizer
        self.options = options
        self._merge_threshold = kwargs.pop("merge_threshold", None)
        self._batch_size = kwargs.pop("batch_size", None)
        self._num_workers = 1
        self._preprocess_params, self._forward_params, self._postprocess_params = self._sanitize_parameters(**kwargs)
        self.call_count = 0
        self.framework = framework
        if self.framework == "pt":
            if isinstance(device, torch.device):
                self.device = device
            elif isinstance(device, str):
                self.device = torch.device(device)
            elif device < 0:
                self.device = torch.device("cpu")
            else:
                self.device = torch.device(f"cuda:{device}")
        else:
            self.device = device

        super(Pipeline, self).__init__()
        self.vad_model = vad

    def _sanitize_parameters(self, **kwargs):
        preprocess_kwargs = {}
        if "tokenizer" in kwargs:
            preprocess_kwargs["maybe_arg"] = kwargs["maybe_arg"]
        return preprocess_kwargs, {}, {}

    def preprocess(self, audio):
        audio = audio['inputs']
        features = log_mel_spectrogram(audio, padding=N_SAMPLES - audio.shape[0])
        return {'inputs': features}

    def _forward(self, model_inputs):
        outputs = self.model.generate_segment_batched(model_inputs['inputs'], self.tokenizer, self.options)
        return {'text': outputs}

    def postprocess(self, model_outputs):
        return model_outputs

    def get_iterator(
        self, inputs, num_workers: int, batch_size: int, preprocess_params, forward_params, postprocess_params
    ):
        dataset = PipelineIterator(inputs, self.preprocess, preprocess_params)
        if "TOKENIZERS_PARALLELISM" not in os.environ:
            os.environ["TOKENIZERS_PARALLELISM"] = "false"
        # TODO hack by collating feature_extractor and image_processor

        def stack(items):
            return {'inputs': torch.stack([x['inputs'] for x in items])}
        dataloader = torch.utils.data.DataLoader(dataset, num_workers=num_workers, batch_size=batch_size, collate_fn=stack)
        model_iterator = PipelineIterator(dataloader, self.forward, forward_params, loader_batch_size=batch_size)
        final_iterator = PipelineIterator(model_iterator, self.postprocess, postprocess_params)
        return final_iterator

    def transcribe(
        #self, audio: Union[str, np.ndarray], batch_size=None, num_workers=0, language=None, task=None
        self, audio: Union[str, np.ndarray], batch_size=None, merge_threshold=None, num_workers=0, language=None, task=None   
    ) -> TranscriptionResult:
        if isinstance(audio, str):
            audio = load_audio(audio)

        def data(audio, segments):
            for seg in segments:
                f1 = int(seg['start'] * SAMPLE_RATE)
                f2 = int(seg['end'] * SAMPLE_RATE)
                # print(f2-f1)
                yield {'inputs': audio[f1:f2]}

        vad_segments = self.vad_model({"waveform": torch.from_numpy(audio).unsqueeze(0), "sample_rate": SAMPLE_RATE})
        #vad_segments = merge_chunks(vad_segments, 30)
        #vad_segments = merge_chunks(vad_segments, 6)  #MJ edit july 31, 2023
        #vad_segments = merge_chunks(vad_segments, 10)  #MJ edit july 31, 2023
        merge_threshold = merge_threshold or self._merge_threshold
        vad_segments = merge_chunks(vad_segments, merge_threshold)  #MJ edit july 31, 2023

        if self.tokenizer is None:
            language = language or self.detect_language(audio)
            task = task or "transcribe"
            self.tokenizer = faster_whisper.tokenizer.Tokenizer(self.model.hf_tokenizer,
                                                                self.model.model.is_multilingual, task=task,
                                                                language=language)
        else:
            language = language or self.tokenizer.language_code
            task = task or self.tokenizer.task
            if task != self.tokenizer.task or language != self.tokenizer.language_code:
                self.tokenizer = faster_whisper.tokenizer.Tokenizer(self.model.hf_tokenizer,
                                                                    self.model.model.is_multilingual, task=task,
                                                                    language=language)

        segments: List[SingleSegment] = []
        batch_size = batch_size or self._batch_size
        for idx, out in enumerate(self.__call__(data(audio, vad_segments), batch_size=batch_size, num_workers=num_workers)):
            text = out['text']
            if batch_size in [0, 1, None]:
                text = text[0]
            segments.append(
                {
                    "text": text,
                    "start": round(vad_segments[idx]['start'], 3),
                    "end": round(vad_segments[idx]['end'], 3)
                }
            )

        return {"segments": segments, "language": language}


    def detect_language(self, audio: np.ndarray):
        if audio.shape[0] < N_SAMPLES:
            print("Warning: audio is shorter than 30s, language detection may be inaccurate.")
        segment = log_mel_spectrogram(audio[: N_SAMPLES],
                                      padding=0 if audio.shape[0] >= N_SAMPLES else N_SAMPLES - audio.shape[0])
        encoder_output = self.model.encode(segment)
        results = self.model.model.detect_language(encoder_output)
        language_token, language_probability = results[0][0]
        language = language_token[2:-2]
        print(f"Detected language: {language} ({language_probability:.2f}) in first 30s of audio...")
        return language
