import os

if "LMQL_BROWSER" in os.environ:
    # use mocked aiohttp for browser (redirects to JS for network requests)
    import lmql.runtime.maiohttp as aiohttp
else:
    # use real aiohttp for python
    import aiohttp

import json
import time
import asyncio

from lmql.runtime.tokenizer import load_tokenizer
from lmql.runtime.stats import Stats

class OpenAIStreamError(Exception): pass
class OpenAIRateLimitError(OpenAIStreamError): pass

class Capacity: pass
Capacity.total = 32000 # defines the total capacity available to allocate to different token streams that run in parallel
# a value of 80000 averages around 130k tok/min on davinci with beam_var (lower values will decrease the rate and avoid rate limiting)
Capacity.reserved = 0

stream_semaphore = None

api_stats = Stats("openai-api")

class CapacitySemaphore:
    def __init__(self, capacity):
        self.capacity = capacity

    async def __aenter__(self):
        # wait for self.capacity > capacity
        while True:
            if Capacity.reserved >= Capacity.total:
                await asyncio.sleep(0.5)
            else:
                Capacity.reserved += self.capacity
                break

    async def __aexit__(self, *args):
        Capacity.reserved -= self.capacity

class concurrent:
    def __init__(self, task):
        self.task = task

    def __enter__(self):
        self.task = asyncio.create_task(self.task)
        return self
    
    def __exit__(self, *args):
        self.task.cancel()

async def complete(**kwargs):
    if kwargs["model"].startswith("gpt-3.5-turbo") or "gpt-4" in kwargs["model"]:
        async for r in chat_api(**kwargs): yield r
    else:
        async for r in completion_api(**kwargs): yield r

global tokenizer
tokenizer = None

def tokenize_ids(text):
    global tokenizer
    if tokenizer is None:
        tokenizer = load_tokenizer("gpt2")
    ids = tokenizer(text)["input_ids"]
    return ids

def tokenize(text):
    global tokenizer
    if tokenizer is None:
        tokenizer = load_tokenizer("gpt2")
    ids = tokenizer(text)["input_ids"]
    raw = tokenizer.decode_bytes(ids)
    raw = [str(t)[2:-1] for t in raw]
    return [t if not "\\x" in t else "bytes:" + t for t in raw]

def tagged_segments(s):
    import re
    segments = []
    current_tag = None
    offset = 0
    for m in re.finditer(r"<lmql:(.*?)\/>", s):
        if m.start() - offset > 0:
            segments.append({"tag": current_tag, "text": s[offset:m.start()]})
        current_tag = m.group(1)
        offset = m.end()
    segments.append({"tag": current_tag, "text": s[offset:]})
    return segments


def get_azure_config(model, api_config):
    endpoint = api_config.get("endpoint", None)

    # first check endpoint configuration
    if endpoint is not None and endpoint.startswith("azure:"):
        endpoint = "https:" + endpoint[6:]
        env_name = model.upper().replace(".", "_")
        
        azure_key = api_config.get("azure_api_key", None)
        if azure_key is None:
            assert "AZURE_OPENAI_" + env_name + "_KEY" in os.environ, f"Please set the environment variable AZURE_OPENAI_{env_name}_KEY to your Azure API key, or specify it in code as 'lmql.model(..., azure_api_key=...')"
            azure_key = os.environ["AZURE_OPENAI_" + env_name + "_KEY"]
        
        headers = {
            "Content-Type": "application/json",
            "api-key": azure_key,
        }

        return endpoint, headers

    # then check env
    if os.environ.get("OPENAI_API_TYPE", 'openai') == 'azure':
        model_env_name = model.upper().replace(".", "_")
        endpoint = os.environ[f"AZURE_OPENAI_{model_env_name}_ENDPOINT"]
        assert "AZURE_OPENAI_" + model_env_name + "_KEY" in os.environ, f"Please set the environment variable AZURE_OPENAI_{model_env_name}_KEY to your Azure API key, or specify it in code as 'lmql.model(..., azure_api_key=...')"
        key = os.environ[f"AZURE_OPENAI_{model_env_name}_KEY"]
        headers = {
            "Content-Type": "application/json",
            "api-key": key,
        }
        return endpoint, headers

def get_endpoint_and_headers(kwargs):
    model = kwargs["model"]
    api_config = kwargs.pop("api_config", {})
    endpoint = api_config.get("endpoint", None)

    # try to get azure config from endpoint or env
    azure_config = get_azure_config(model, api_config)
    if azure_config is not None:
        return azure_config
    
    # otherwise use custom endpoint as plain URL without authorization
    if endpoint is not None:
        if not endpoint.endswith("http"):
            endpoint = f"http://{endpoint}"
        return endpoint + "/completions", {
            "Content-Type": "application/json"
        }
    
    # use standard public API config
    from lmql.runtime.openai_secret import openai_secret, openai_org
    if kwargs["model"].startswith("gpt-3.5-turbo") or "gpt-4" in kwargs["model"]:
        endpoint = "https://api.openai.com/v1/chat/completions"
    else:
        endpoint = "https://api.openai.com/v1/completions"
    return endpoint, {
        "Authorization": f"Bearer {openai_secret}",
        "Content-Type": "application/json",
    }

async def chat_api(**kwargs):
    global stream_semaphore

    num_prompts = len(kwargs["prompt"])
    max_tokens = kwargs.get("max_tokens", 0)

    assert "logit_bias" not in kwargs.keys(), f"Chat API models do not support advanced constraining of the output, please use no or less complicated constraints."
    prompt_tokens = tokenize(kwargs["prompt"][0])

    timeout = kwargs.pop("timeout", 1.5)
    
    echo = kwargs.pop("echo")

    if echo:
        data = {
            "choices": [
                {
                    "text": kwargs["prompt"][0],
                    "index": 0,
                    "finish_reason": None,
                    "logprobs": {
                        "text_offset": [0 for t in prompt_tokens],
                        "token_logprobs": [0.0 for t in prompt_tokens],
                        "tokens": prompt_tokens,
                        "top_logprobs": [{t: 0.0} for t in prompt_tokens]
                    }
                }
            ]
        }
        yield data
    
    if max_tokens == 0:
        return
    
    assert len(kwargs["prompt"]) == 1, f"chat API models do not support batched processing"
    
    messages = []
    for s in tagged_segments(kwargs["prompt"][0]):
        role = "user"
        tag = s["tag"]
        if tag == "system":
            role = "system"
        elif tag == "assistant":
            role = "assistant"
        elif tag == "user":
            role = "user"
        elif tag is None:
            role = "user"
        else:
            print(f"warning: {tag} is not a valid tag for the OpenAI chat API. Please use one of :system, :user or :assistant.")
        
        messages.append({
            "role": role, 
            "content": s["text"]
        })
    
    del kwargs["prompt"]
    kwargs["messages"] = messages
    
    del kwargs["logprobs"]

    async with CapacitySemaphore(num_prompts * max_tokens):
        
        current_chunk = ""
        stream_start = time.time()
        
        async with aiohttp.ClientSession() as session:
            endpoint, headers = get_endpoint_and_headers(kwargs)
            async with session.post(
                    endpoint,
                    headers=headers,
                    json={**kwargs},
            ) as resp:
                last_chunk_time = time.time()
                sum_chunk_times = 0
                n_chunks = 0
                current_chunk_time = 0

                async def chunk_timer():
                    nonlocal last_chunk_time, sum_chunk_times, n_chunks, current_chunk_time
                    while True:
                        await asyncio.sleep(0.5)
                        current_chunk_time = time.time() - last_chunk_time
                        # print("Average chunk time:", sum_chunk_times / n_chunks, "Current chunk time:", current_chunk_time)
                        # print("available capacity", Capacity.total - Capacity.reserved, "reserved capacity", Capacity.reserved, "total capacity", Capacity.total, flush=True)

                        if current_chunk_time > timeout:
                            print("Token stream took too long to produce next chunk, re-issuing completion request. Average chunk time:", sum_chunk_times / max(1,n_chunks), "Current chunk time:", current_chunk_time, flush=True)
                            resp.close()
                            raise OpenAIStreamError("Token stream took too long to produce next chunk.")

                received_text = ""

                with concurrent(chunk_timer()):
                    async for chunk in resp.content.iter_any():
                        chunk = chunk.decode("utf-8")
                        current_chunk += chunk
                        is_done = current_chunk.strip().endswith("[DONE]")
                        
                        while "data: " in current_chunk:
                            chunks = current_chunk.split("\ndata: ")
                            while len(chunks[0]) == 0:
                                chunks = chunks[1:]
                            if len(chunks) == 1:
                                # last chunk may be incomplete
                                break
                            complete_chunk = chunks[0].strip()
                            current_chunk = "\ndata: ".join(chunks[1:])

                            if complete_chunk.startswith("data: "):
                                complete_chunk = complete_chunk[len("data: "):]

                            if len(complete_chunk.strip()) == 0: 
                                continue
                            if complete_chunk == "[DONE]": 
                                return

                            n_chunks += 1
                            sum_chunk_times += time.time() - last_chunk_time
                            last_chunk_time = time.time()
                            
                            try:
                                data = json.loads(complete_chunk)
                            except json.decoder.JSONDecodeError:
                                print("Failed to decode JSON:", [complete_chunk])

                            if "error" in data.keys():
                                message = data["error"]["message"]
                                if "rate limit" in message.lower():
                                    raise OpenAIRateLimitError(message + "local client capacity" + str(Capacity.reserved))
                                else:
                                    raise OpenAIStreamError(message + " (after receiving " + str(n_chunks) + " chunks. Current chunk time: " + str(time.time() - last_chunk_time) + " Average chunk time: " + str(sum_chunk_times / max(1, n_chunks)) + ")", "Stream duration:", time.time() - stream_start)

                            choices = []
                            for c in data["choices"]:
                                delta = c["delta"]
                                # skip non-content annotations for now
                                if not "content" in delta:
                                    if len(delta) == 0: # {} indicates end of stream
                                        choices.append({
                                            "text": "",
                                            "index": c["index"],
                                            "finish_reason": c["finish_reason"],
                                            "logprobs": {
                                                "text_offset": [],
                                                "token_logprobs": [],
                                                "tokens": [],
                                                "top_logprobs": []
                                            }
                                        })
                                    continue
                                text = delta["content"]
                                tokens = tokenize((" " if received_text == "" else "") + text)
                                received_text += text
                                
                                choices.append({
                                    "text": text,
                                    "index": c["index"],
                                    "finish_reason": c["finish_reason"],
                                    "logprobs": {
                                        "text_offset": [0 for _ in range(len(tokens))],
                                        "token_logprobs": [0.0 for _ in range(len(tokens))],
                                        "tokens": tokens,
                                        "top_logprobs": [{t: 0.0} for t in tokens]
                                    }
                                })
                            data["choices"] = choices

                            yield data
                        
                        if is_done: break
                    
                resp.close()

                if current_chunk.strip() == "[DONE]":
                    return
                try:
                    last_message = json.loads(current_chunk.strip())
                    message = last_message.get("error", {}).get("message", "")
                    if "rate limit" in message.lower():
                        raise OpenAIRateLimitError(message + "local client capacity" + str(Capacity.reserved))
                    else:
                        raise OpenAIStreamError(last_message["error"]["message"] + " (after receiving " + str(n_chunks) + " chunks. Current chunk time: " + str(time.time() - last_chunk_time) + " Average chunk time: " + str(sum_chunk_times / max(1, n_chunks)) + ")", "Stream duration:", time.time() - stream_start)
                        # raise OpenAIStreamError(last_message["error"]["message"])
                except json.decoder.JSONDecodeError:
                    raise OpenAIStreamError("Error in API response:", current_chunk)
    
async def completion_api(**kwargs):
    global stream_semaphore

    num_prompts = len(kwargs["prompt"])
    max_tokens = kwargs.get("max_tokens", 0)

    timeout = kwargs.pop("timeout", 1.5)

    async with CapacitySemaphore(num_prompts * max_tokens):
        
        current_chunk = ""
        stream_start = time.time()
        
        async with aiohttp.ClientSession() as session:
            endpoint, headers = get_endpoint_and_headers(kwargs)
            async with session.post(
                    endpoint,
                    headers=headers,
                    json={**kwargs},
            ) as resp:
                last_chunk_time = time.time()
                sum_chunk_times = 0
                n_chunks = 0
                current_chunk_time = 0

                async def chunk_timer():
                    nonlocal last_chunk_time, sum_chunk_times, n_chunks, current_chunk_time
                    while True:
                        await asyncio.sleep(0.5)
                        current_chunk_time = time.time() - last_chunk_time
                        # print("Average chunk time:", sum_chunk_times / n_chunks, "Current chunk time:", current_chunk_time)
                        # print("available capacity", Capacity.total - Capacity.reserved, "reserved capacity", Capacity.reserved, "total capacity", Capacity.total, flush=True)

                        if current_chunk_time > timeout:
                            print("Token stream took too long to produce next chunk, re-issuing completion request. Average chunk time:", sum_chunk_times / max(1,n_chunks), "Current chunk time:", current_chunk_time, flush=True)
                            resp.close()
                            raise OpenAIStreamError("Token stream took too long to produce next chunk.")

                with concurrent(chunk_timer()):
                    async for chunk in resp.content.iter_any():
                        chunk = chunk.decode("utf-8")
                        current_chunk += chunk
                        is_done = current_chunk.strip().endswith("[DONE]")
                        
                        while "data: " in current_chunk:
                            chunks = current_chunk.split("\ndata: ")
                            while len(chunks[0]) == 0:
                                chunks = chunks[1:]
                            if len(chunks) == 1:
                                # last chunk may be incomplete
                                break
                            complete_chunk = chunks[0].strip()
                            current_chunk = "\ndata: ".join(chunks[1:])
                            
                            if complete_chunk.startswith("data: "):
                                complete_chunk = complete_chunk[len("data: "):]

                            if len(complete_chunk.strip()) == 0: 
                                continue
                            if complete_chunk == "[DONE]":
                                return
                            
                            if n_chunks == 0:
                                api_stats.times["first-chunk-latency"] = api_stats.times.get("first-chunk-latency", 0) + (time.time() - stream_start)

                            n_chunks += 1
                            sum_chunk_times += time.time() - last_chunk_time
                            last_chunk_time = time.time()
                            
                            try:
                                data = json.loads(complete_chunk)
                            except json.decoder.JSONDecodeError:
                                print("Failed to decode JSON:", [complete_chunk])

                            if "error" in data.keys():
                                message = data["error"]["message"]
                                if "rate limit" in message.lower():
                                    raise OpenAIRateLimitError(message + "local client capacity" + str(Capacity.reserved))
                                else:
                                    raise OpenAIStreamError(message + " (after receiving " + str(n_chunks) + " chunks. Current chunk time: " + str(time.time() - last_chunk_time) + " Average chunk time: " + str(sum_chunk_times / max(1, n_chunks)) + ")", "Stream duration:", time.time() - stream_start)

                            yield data
                        
                        if is_done: break
                    
                resp.close()

                if current_chunk.strip() == "[DONE]":
                    return
                
                try:
                    last_message = json.loads(current_chunk.strip())
                    message = last_message.get("error", {}).get("message", "")
                    if "rate limit" in message.lower():
                        raise OpenAIRateLimitError(message + "local client capacity" + str(Capacity.reserved))
                    else:
                        raise OpenAIStreamError(last_message["error"]["message"] + " (after receiving " + str(n_chunks) + " chunks. Current chunk time: " + str(time.time() - last_chunk_time) + " Average chunk time: " + str(sum_chunk_times / max(1, n_chunks)) + ")", "Stream duration:", time.time() - stream_start)
                        # raise OpenAIStreamError(last_message["error"]["message"])
                except json.decoder.JSONDecodeError:
                    raise OpenAIStreamError("Error in API response:", current_chunk)

async def main():
    import sys
    # Not sure if this should work, but prompt needs tokenizing I think
    """
    kwargs = {
        "model": "text-davinci-003",
        "prompt": "Say this is a test",
        "max_tokens": 7,
        "temperature": 0,
        "stream": True
    }

    async for chunk in complete(**kwargs):
                print(chunk)"""

    """
     Tested working with these environment variables:
        Azure config for GPT-3.5-Turbo:
            OPENAI_API_TYPE = azure
            AZURE_OPENAI_GPT-3_5-TURBO_ENDPOINT = https://{service}.openai.azure.com/openai/deployments/{gpt3.5-turbo-deployment}/chat/completions?api-version=2023-03-15-preview
            AZURE_OPENAI_GPT-3_5-TURBO_KEY = XXXXXXXXX
        Regular OpenAI credentials for GPT-3.5-Turbo:
            OPENAI_API_TYPE = openai
            OPENAI_API_KEY = XXXXXXXXX
    """

    kwargs = {
        "model": "gpt-3.5-turbo",
        "prompt": [
            tokenize("<lmql:system/> You are a helpful assistant.<lmql:user/>Hi, tell me all you know about GPT-2.")],
        "max_tokens": 512,
        "temperature": 0.,
        "stream": True,
        "echo": False,
        "logprobs": None,
    }

    async for chunk in chat_api(**kwargs):
        if len(chunk["choices"]) > 0:
            sys.stdout.write(chunk["choices"][0]["text"])


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
