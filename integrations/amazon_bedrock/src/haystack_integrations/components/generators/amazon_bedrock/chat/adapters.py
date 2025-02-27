import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Callable, ClassVar, Dict, List, Optional

from botocore.eventstream import EventStream
from haystack.components.generators.openai_utils import _convert_message_to_openai_format
from haystack.dataclasses import ChatMessage, ChatRole, StreamingChunk
from transformers import AutoTokenizer, PreTrainedTokenizer

from haystack_integrations.components.generators.amazon_bedrock.handlers import DefaultPromptHandler

logger = logging.getLogger(__name__)


class BedrockModelChatAdapter(ABC):
    """
    Base class for Amazon Bedrock chat model adapters.

    Each subclass of this class is designed to address the unique specificities of a particular chat LLM it adapts,
    focusing on preparing the requests and extracting the responses from the Amazon Bedrock hosted chat LLMs.
    """

    def __init__(self, truncate: Optional[bool], generation_kwargs: Dict[str, Any]) -> None:
        """
        Initializes the chat adapter with the truncate parameter and generation kwargs.
        """
        self.generation_kwargs = generation_kwargs
        self.truncate = truncate

    @abstractmethod
    def prepare_body(self, messages: List[ChatMessage], **inference_kwargs) -> Dict[str, Any]:
        """
        Prepares the body for the Amazon Bedrock request.
        Subclasses should override this method to package the chat messages into the request.

        :param messages: The chat messages to package into the request.
        :param inference_kwargs: Additional inference kwargs to use.
        :returns: The prepared body.
        """

    def get_responses(self, response_body: Dict[str, Any]) -> List[ChatMessage]:
        """
        Extracts the responses from the Amazon Bedrock response.

        :param response_body: The response body.
        :returns: The extracted responses.
        """
        return self._extract_messages_from_response(response_body)

    def get_stream_responses(
        self, stream: EventStream, streaming_callback: Callable[[StreamingChunk], None]
    ) -> List[ChatMessage]:
        streaming_chunks: List[StreamingChunk] = []
        last_decoded_chunk: Dict[str, Any] = {}
        for event in stream:
            chunk = event.get("chunk")
            if chunk:
                last_decoded_chunk = json.loads(chunk["bytes"].decode("utf-8"))
                streaming_chunk = self._build_streaming_chunk(last_decoded_chunk)
                streaming_callback(streaming_chunk)  # callback the stream handler with StreamingChunk
                streaming_chunks.append(streaming_chunk)
        responses = ["".join(chunk.content for chunk in streaming_chunks).lstrip()]
        return [ChatMessage.from_assistant(response, meta=last_decoded_chunk) for response in responses]

    @staticmethod
    def _update_params(target_dict: Dict[str, Any], updates_dict: Dict[str, Any], allowed_params: List[str]) -> None:
        """
        Updates target_dict with values from updates_dict. Merges lists instead of overriding them.

        :param target_dict: The dictionary to update.
        :param updates_dict: The dictionary with updates.
        :param allowed_params: The list of allowed params to use.
        """
        for key, value in updates_dict.items():
            if key not in allowed_params:
                logger.warning(f"Parameter '{key}' is not allowed and will be ignored.")
                continue
            if key in target_dict and isinstance(target_dict[key], list) and isinstance(value, list):
                # Merge lists and remove duplicates
                target_dict[key] = sorted(set(target_dict[key] + value))
            else:
                # Override the value in target_dict
                target_dict[key] = value

    def _get_params(
        self, inference_kwargs: Dict[str, Any], default_params: Dict[str, Any], allowed_params: List[str]
    ) -> Dict[str, Any]:
        """
        Merges params from inference_kwargs with the default params and self.generation_kwargs.
        Uses a helper function to merge lists or override values as necessary.

        :param inference_kwargs: The inference kwargs to merge.
        :param default_params: The default params to start with.
        :param allowed_params: The list of allowed params to use.
        :returns: The merged params.
        """
        # Start with a copy of default_params
        kwargs = default_params.copy()

        # Update the default params with self.generation_kwargs and finally inference_kwargs
        self._update_params(kwargs, self.generation_kwargs, allowed_params)
        self._update_params(kwargs, inference_kwargs, allowed_params)

        return kwargs

    def _ensure_token_limit(self, prompt: str) -> str:
        """
        Ensures that the prompt is within the token limit for the model.
        :param prompt: The prompt to check.
        :returns: The resized prompt.
        """
        resize_info = self.check_prompt(prompt)
        if resize_info["prompt_length"] != resize_info["new_prompt_length"]:
            logger.warning(
                "The prompt was truncated from %s tokens to %s tokens so that the prompt length and "
                "the answer length (%s tokens) fit within the model's max token limit (%s tokens). "
                "Shorten the prompt or it will be cut off.",
                resize_info["prompt_length"],
                max(0, resize_info["model_max_length"] - resize_info["max_length"]),  # type: ignore
                resize_info["max_length"],
                resize_info["model_max_length"],
            )
        return str(resize_info["resized_prompt"])

    @abstractmethod
    def check_prompt(self, prompt: str) -> Dict[str, Any]:
        """
        Checks the prompt length and resizes it if necessary. If the prompt is too long, it will be truncated.

        :param prompt: The prompt to check.
        :returns: A dictionary containing the resized prompt and additional information.
        """

    @abstractmethod
    def _extract_messages_from_response(self, response_body: Dict[str, Any]) -> List[ChatMessage]:
        """
        Extracts the messages from the response body.

        :param response_body: The response body.
        :returns: The extracted ChatMessage list.
        """

    @abstractmethod
    def _build_streaming_chunk(self, chunk: Dict[str, Any]) -> StreamingChunk:
        """
        Extracts the content and meta from a streaming chunk.

        :param chunk: The streaming chunk as dict.
        :returns: A StreamingChunk object.
        """


class AnthropicClaudeChatAdapter(BedrockModelChatAdapter):
    """
    Model adapter for the Anthropic Claude chat model.
    """

    # https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-anthropic-claude-messages.html
    ALLOWED_PARAMS: ClassVar[List[str]] = [
        "anthropic_version",
        "max_tokens",
        "stop_sequences",
        "temperature",
        "top_p",
        "top_k",
        "system",
        "tools",
        "tool_choice",
    ]

    def __init__(self, truncate: Optional[bool], generation_kwargs: Dict[str, Any]):
        """
        Initializes the Anthropic Claude chat adapter.

        :param truncate: Whether to truncate the prompt if it exceeds the model's max token limit.
        :param generation_kwargs: The generation kwargs.
        """
        super().__init__(truncate, generation_kwargs)

        # We pop the model_max_length as it is not sent to the model
        # but used to truncate the prompt if needed
        # Anthropic Claude has a limit of at least 100000 tokens
        # https://docs.anthropic.com/claude/reference/input-and-output-sizes
        model_max_length = self.generation_kwargs.pop("model_max_length", 100000)

        # Truncate prompt if prompt tokens > model_max_length-max_length
        # (max_length is the length of the generated text)
        # TODO use Anthropic tokenizer to get the precise prompt length
        # See https://github.com/anthropics/anthropic-sdk-python?tab=readme-ov-file#token-counting
        self.prompt_handler = DefaultPromptHandler(
            tokenizer="gpt2",
            model_max_length=model_max_length,
            max_length=self.generation_kwargs.get("max_tokens") or 512,
        )

    def prepare_body(self, messages: List[ChatMessage], **inference_kwargs) -> Dict[str, Any]:
        """
        Prepares the body for the Anthropic Claude request.

        :param messages: The chat messages to package into the request.
        :param inference_kwargs: Additional inference kwargs to use.
        :returns: The prepared body.
        """
        default_params = {
            "anthropic_version": self.generation_kwargs.get("anthropic_version") or "bedrock-2023-05-31",
            "max_tokens": self.generation_kwargs.get("max_tokens") or 512,  # max_tokens is required
        }

        # combine stop words with default stop sequences, remove stop_words as Anthropic does not support it
        stop_sequences = inference_kwargs.get("stop_sequences", []) + inference_kwargs.pop("stop_words", [])
        if stop_sequences:
            inference_kwargs["stop_sequences"] = stop_sequences
        # pop stream kwarg from inference_kwargs as Anthropic does not support it (if provided)
        inference_kwargs.pop("stream", None)
        params = self._get_params(inference_kwargs, default_params, self.ALLOWED_PARAMS)
        body = {**self.prepare_chat_messages(messages=messages), **params}
        return body

    def prepare_chat_messages(self, messages: List[ChatMessage]) -> Dict[str, Any]:
        """
        Prepares the chat messages for the Anthropic Claude request.

        :param messages: The chat messages to prepare.
        :returns: The prepared chat messages as a dictionary.
        """
        body: Dict[str, Any] = {}
        system = messages[0].content if messages and messages[0].is_from(ChatRole.SYSTEM) else None
        body["messages"] = [
            self._to_anthropic_message(m) for m in messages if m.is_from(ChatRole.USER) or m.is_from(ChatRole.ASSISTANT)
        ]
        if system:
            body["system"] = system
        # Ensure token limit for each message in the body
        if self.truncate:
            for message in body["messages"]:
                for content in message["content"]:
                    content["text"] = self._ensure_token_limit(content["text"])
        return body

    def check_prompt(self, prompt: str) -> Dict[str, Any]:
        """
        Checks the prompt length and resizes it if necessary. If the prompt is too long, it will be truncated.

        :param prompt: The prompt to check.
        :returns: A dictionary containing the resized prompt and additional information.
        """
        return self.prompt_handler(prompt)

    def _extract_messages_from_response(self, response_body: Dict[str, Any]) -> List[ChatMessage]:
        """
        Extracts the messages from the response body.

        :param response_body: The response body.
        :return: The extracted ChatMessage list.
        """
        messages: List[ChatMessage] = []
        if response_body.get("type") == "message":
            if response_body.get("stop_reason") == "tool_use":  # If `tool_use` we only keep the tool_use content
                for content in response_body["content"]:
                    if content.get("type") == "tool_use":
                        meta = {k: v for k, v in response_body.items() if k not in ["type", "content", "role"]}
                        json_answer = json.dumps(content)
                        messages.append(ChatMessage.from_assistant(json_answer, meta=meta))
            else:  # For other stop_reason, return all text content
                for content in response_body["content"]:
                    if content.get("type") == "text":
                        meta = {k: v for k, v in response_body.items() if k not in ["type", "content", "role"]}
                        messages.append(ChatMessage.from_assistant(content["text"], meta=meta))

        return messages

    def _build_streaming_chunk(self, chunk: Dict[str, Any]) -> StreamingChunk:
        """
        Extracts the content and meta from a streaming chunk.

        :param chunk: The streaming chunk as dict.
        :returns: A StreamingChunk object.
        """
        if chunk.get("type") == "content_block_delta" and chunk.get("delta", {}).get("type") == "text_delta":
            return StreamingChunk(content=chunk.get("delta", {}).get("text", ""), meta=chunk)
        return StreamingChunk(content="", meta=chunk)

    def _to_anthropic_message(self, m: ChatMessage) -> Dict[str, Any]:
        """
        Convert a ChatMessage to a dictionary with the content and role fields.
        :param m: The ChatMessage to convert.
        :return: The dictionary with the content and role fields.
        """
        return {"content": [{"type": "text", "text": m.content}], "role": m.role.value}


class MistralChatAdapter(BedrockModelChatAdapter):
    """
    Model adapter for the Mistral chat model.
    """

    chat_template = """
    {% if messages[0]['role'] == 'system' %}
        {% set loop_messages = messages[1:] %}
        {% set system_message = messages[0]['content'] %}
    {% else %}
        {% set loop_messages = messages %}
        {% set system_message = false %}
    {% endif %}
    {{bos_token}}
    {% for message in loop_messages %}
        {% if (message['role'] == 'user') != (loop.index0 % 2 == 0) %}
            {{ raise_exception('Conversation roles must alternate user/assistant/user/assistant/...') }}
        {% endif %}
        {% if loop.index0 == 0 and system_message != false %}
            {% set content = system_message + '\n' + message['content'] %}
        {% else %}
            {% set content = message['content'] %}
        {% endif %}
        {% if message['role'] == 'user' %}
            {{ '[INST] ' + content.strip() + ' [/INST]' }}
        {% elif message['role'] == 'assistant' %}
            {{ content.strip() + eos_token }}
        {% endif %}
    {% endfor %}
    """
    chat_template = "".join(line.strip() for line in chat_template.splitlines())

    # the above template was designed to match https://docs.mistral.ai/models/#chat-template
    # and to support system messages, otherwise we could use the default mistral chat template
    # available on HF infrastructure

    # https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-anthropic-claude-messages.html
    ALLOWED_PARAMS: ClassVar[List[str]] = [
        "max_tokens",
        "safe_prompt",
        "random_seed",
        "temperature",
        "top_p",
    ]

    def __init__(self, truncate: Optional[bool], generation_kwargs: Dict[str, Any]):
        """
        Initializes the Mistral chat adapter.
        :param truncate: Whether to truncate the prompt if it exceeds the model's max token limit.
        :param generation_kwargs: The generation kwargs.
        """
        super().__init__(truncate, generation_kwargs)

        # We pop the model_max_length as it is not sent to the model
        # but used to truncate the prompt if needed
        # Mistral has a limit of at least 32000 tokens
        model_max_length = self.generation_kwargs.pop("model_max_length", 32000)

        # Use `mistralai/Mistral-7B-v0.1` as tokenizer, all mistral models likely use the same tokenizer
        # a) we should get good estimates for the prompt length
        # b) we can use apply_chat_template with the template above to delineate ChatMessages
        # Mistral models are gated on HF Hub. If no HF_TOKEN is found we use a non-gated alternative tokenizer model.
        tokenizer: PreTrainedTokenizer
        if os.environ.get("HF_TOKEN") or os.environ.get("HF_API_TOKEN"):
            tokenizer = AutoTokenizer.from_pretrained("mistralai/Mistral-7B-Instruct-v0.1")
        else:
            tokenizer = AutoTokenizer.from_pretrained("NousResearch/Llama-2-7b-chat-hf")
            logger.warning(
                "Gated mistralai/Mistral-7B-Instruct-v0.1 model cannot be used as a tokenizer for "
                "estimating the prompt length because no HF_TOKEN was found. Using "
                "NousResearch/Llama-2-7b-chat-hf instead. To use a mistral tokenizer export an env var "
                "HF_TOKEN containing a Hugging Face token and make sure you have access to the model."
            )

        self.prompt_handler = DefaultPromptHandler(
            tokenizer=tokenizer,
            model_max_length=model_max_length,
            max_length=self.generation_kwargs.get("max_tokens") or 512,
        )

    def prepare_body(self, messages: List[ChatMessage], **inference_kwargs) -> Dict[str, Any]:
        """
        Prepares the body for the Mistral request.

        :param messages: The chat messages to package into the request.
        :param inference_kwargs: Additional inference kwargs to use.
        :returns: The prepared body.
        """
        default_params = {
            "max_tokens": self.generation_kwargs.get("max_tokens") or 512,  # max_tokens is required
        }
        # replace stop_words from inference_kwargs with stop, as this is Mistral specific parameter
        stop_words = inference_kwargs.pop("stop_words", [])
        if stop_words:
            inference_kwargs["stop"] = stop_words

        # pop stream kwarg from inference_kwargs as Mistral does not support it (if provided)
        inference_kwargs.pop("stream", None)

        params = self._get_params(inference_kwargs, default_params, self.ALLOWED_PARAMS)
        body = {"prompt": self.prepare_chat_messages(messages=messages), **params}
        return body

    def prepare_chat_messages(self, messages: List[ChatMessage]) -> str:
        """
        Prepares the chat messages for the Mistral request.

        :param messages: The chat messages to prepare.
        :returns: The prepared chat messages as a string.
        """
        # it would be great to use the default mistral chat template, but it doesn't support system messages
        # the class variable defined chat_template is a workaround to support system messages
        # default is https://huggingface.co/mistralai/Mixtral-8x7B-Instruct-v0.1/blob/main/tokenizer_config.json
        # but we'll use our custom chat template
        prepared_prompt: str = self.prompt_handler.tokenizer.apply_chat_template(
            conversation=[_convert_message_to_openai_format(m) for m in messages],
            tokenize=False,
            chat_template=self.chat_template,
        )
        if self.truncate:
            prepared_prompt = self._ensure_token_limit(prepared_prompt)
        return prepared_prompt

    def check_prompt(self, prompt: str) -> Dict[str, Any]:
        """
        Checks the prompt length and resizes it if necessary. If the prompt is too long, it will be truncated.

        :param prompt: The prompt to check.
        :returns: A dictionary containing the resized prompt and additional information.
        """
        return self.prompt_handler(prompt)

    def _extract_messages_from_response(self, response_body: Dict[str, Any]) -> List[ChatMessage]:
        """
        Extracts the messages from the response body.

        :param response_body: The response body.
        :return: The extracted ChatMessage list.
        """
        messages: List[ChatMessage] = []
        responses = response_body.get("outputs", [])
        for response in responses:
            meta = {k: v for k, v in response.items() if k not in ["text"]}
            messages.append(ChatMessage.from_assistant(response["text"], meta=meta))
        return messages

    def _build_streaming_chunk(self, chunk: Dict[str, Any]) -> StreamingChunk:
        """
        Extracts the content and meta from a streaming chunk.

        :param chunk: The streaming chunk as dict.
        :returns: A StreamingChunk object.
        """
        response_chunk = chunk.get("outputs", [])
        if response_chunk:
            return StreamingChunk(content=response_chunk[0].get("text", ""), meta=chunk)
        return StreamingChunk(content="", meta=chunk)


class MetaLlama2ChatAdapter(BedrockModelChatAdapter):
    """
    Model adapter for the Meta Llama 2 models.
    """

    # https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-meta.html
    ALLOWED_PARAMS: ClassVar[List[str]] = ["max_gen_len", "temperature", "top_p"]

    chat_template = (
        "{% if messages[0]['role'] == 'system' %}"
        "{% set loop_messages = messages[1:] %}"
        "{% set system_message = messages[0]['content'] %}"
        "{% else %}"
        "{% set loop_messages = messages %}"
        "{% set system_message = false %}"
        "{% endif %}"
        "{% for message in loop_messages %}"
        "{% if (message['role'] == 'user') != (loop.index0 % 2 == 0) %}"
        "{{ raise_exception('Conversation roles must alternate user/assistant/user/assistant/...') }}"
        "{% endif %}"
        "{% if loop.index0 == 0 and system_message != false %}"
        "{% set content = '<<SYS>>\n' + system_message + '\n<</SYS>>\n\n' + message['content'] %}"
        "{% else %}"
        "{% set content = message['content'] %}"
        "{% endif %}"
        "{% if message['role'] == 'user' %}"
        "{{ bos_token + '[INST] ' + content.strip() + ' [/INST]' }}"
        "{% elif message['role'] == 'assistant' %}"
        "{{ ' '  + content.strip() + ' ' + eos_token }}"
        "{% endif %}"
        "{% endfor %}"
    )

    def __init__(self, truncate: Optional[bool], generation_kwargs: Dict[str, Any]) -> None:
        """
        Initializes the Meta Llama 2 chat adapter.
        :param truncate: Whether to truncate the prompt if it exceeds the model's max token limit.
        :param generation_kwargs: The generation kwargs.
        """
        super().__init__(truncate, generation_kwargs)
        # We pop the model_max_length as it is not sent to the model
        # but used to truncate the prompt if needed
        # Llama 2 has context window size of 4096 tokens
        # with some exceptions when the context window has been extended
        model_max_length = self.generation_kwargs.pop("model_max_length", 4096)

        # Use `google/flan-t5-base` as it's also BPE sentencepiece tokenizer just like llama 2
        # a) we should get good estimates for the prompt length (empirically close to llama 2)
        # b) we can use apply_chat_template with the template above to delineate ChatMessages
        tokenizer: PreTrainedTokenizer = AutoTokenizer.from_pretrained("google/flan-t5-base")
        tokenizer.bos_token = "<s>"
        tokenizer.eos_token = "</s>"
        tokenizer.unk_token = "<unk>"
        self.prompt_handler = DefaultPromptHandler(
            tokenizer=tokenizer,
            model_max_length=model_max_length,
            max_length=self.generation_kwargs.get("max_gen_len") or 512,
        )

    def prepare_body(self, messages: List[ChatMessage], **inference_kwargs) -> Dict[str, Any]:
        """
        Prepares the body for the Meta Llama 2 request.

        :param messages: The chat messages to package into the request.
        :param inference_kwargs: Additional inference kwargs to use.
        """
        default_params = {"max_gen_len": self.generation_kwargs.get("max_gen_len") or 512}

        # no support for stop words in Meta Llama 2
        params = self._get_params(inference_kwargs, default_params, self.ALLOWED_PARAMS)
        body = {"prompt": self.prepare_chat_messages(messages=messages), **params}
        return body

    def prepare_chat_messages(self, messages: List[ChatMessage]) -> str:
        """
        Prepares the chat messages for the Meta Llama 2 request.

        :param messages: The chat messages to prepare.
        :returns: The prepared chat messages as a string ready for the model.
        """
        prepared_prompt: str = self.prompt_handler.tokenizer.apply_chat_template(
            conversation=messages, tokenize=False, chat_template=self.chat_template
        )

        if self.truncate:
            prepared_prompt = self._ensure_token_limit(prepared_prompt)
        return prepared_prompt

    def check_prompt(self, prompt: str) -> Dict[str, Any]:
        """
        Checks the prompt length and resizes it if necessary. If the prompt is too long, it will be truncated.

        :param prompt: The prompt to check.
        :returns: A dictionary containing the resized prompt and additional information.

        """
        return self.prompt_handler(prompt)

    def _extract_messages_from_response(self, response_body: Dict[str, Any]) -> List[ChatMessage]:
        """
        Extracts the messages from the response body.

        :param response_body: The response body.
        :return: The extracted ChatMessage list.
        """
        message_tag = "generation"
        metadata = {k: v for (k, v) in response_body.items() if k != message_tag}
        return [ChatMessage.from_assistant(response_body[message_tag], meta=metadata)]

    def _build_streaming_chunk(self, chunk: Dict[str, Any]) -> StreamingChunk:
        """
        Extracts the content and meta from a streaming chunk.

        :param chunk: The streaming chunk as dict.
        :returns: A StreamingChunk object.
        """
        return StreamingChunk(content=chunk.get("generation", ""), meta=chunk)
