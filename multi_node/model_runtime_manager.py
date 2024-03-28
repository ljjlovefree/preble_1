import requests
from data_parallel_request_cache import (
    DataParallelRuntimeSelectionPolicy,
    ConsistentHashingWithRadixCache,
)
from data_parallel_request_cache import DataParallelRequestRouter
import aiohttp
import uuid
from dataclasses import dataclass
from sglang.srt.server import Runtime as SGLangServer
from typing import List, Iterable
from concurrent.futures import ThreadPoolExecutor
import json
import asyncio
import numpy as np
import time
import paramiko
from ssh_runtime import SSHRuntimeManager

class GPUConfig:
    def __init__(self, gpu_id, url=None, use_ssh=False, ssh_config={}) -> None:
        self.gpu_id = gpu_id
        self.url = url
        self.use_ssh = use_ssh
        self.ssh_config = ssh_config

@dataclass
class EndpointRuntimeInterface:
    def __post_init__(self):
        self.runtime_id = str(uuid.uuid4())
        assert self.url is not None
        self._generate_url = f"{self.url}/generate"

    @property
    def generate_url(self):
        return self._generate_url

    @generate_url.setter
    def generate_url(self, url):
        self._generate_url = url

    @property
    def flush_cache_url(self):
        return f"{self.url}/flush_cache"

    def shutdown(self):
        pass

class URLRuntime(EndpointRuntimeInterface):
    def __init__(self, url, gpu):
        super().__init__()
        self.url = url
        self.gpu = gpu

class ExtendedSGLangRuntime(SGLangServer, EndpointRuntimeInterface):
    def __init__(self, gpu, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.gpu = gpu

class SSHRuntime(SSHRuntimeManager, EndpointRuntimeInterface):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

def random_uuid_string():
    return str(uuid.uuid4().hex)


class ModelDetails:
    """
    Supports Data Parallel Model Allocation
    """

    def __init__(
        self, model_path, gpu_configs, runtime_selection_policy=DataParallelRuntimeSelectionPolicy.RANDOM
    ) -> None:
        self.model_path = model_path
        self.weights = []
        self.runtimes: List[EndpointRuntimeInterface] = []
        self.request_router: DataParallelRequestRouter = DataParallelRequestRouter(
            runtime_selection_policy, total_nodes=len(gpu_configs)
        )
        # self.gpus = set(gpus)
        self.gpu_configs = gpu_configs
        self.start_time = None
        self.request_sent_time = []
        self.current_experiment_state_time = None

    # TODO Load runtimes in parallel to reduce cold start time
        # Potentially extract this to the parent model node loder to effeciently load multiple models in parallel
    def load_runtimes(self, model_path, gpu_configs, **kwargs):
        print(kwargs)
        def load_runtime(config: GPUConfig):
            runtime: EndpointRuntimeInterface
            gpu_id = config.gpu_id
            if config.use_ssh:
                runtime = SSHRuntime(
                    model_path=model_path,
                    ssh_config=config.ssh_config,
                    gpu=gpu_id,
                    cuda_devices=gpu_id,
                    context_length=4096,
                    **kwargs
                )
            elif config.url:
                runtime = URLRuntime(
                    config.url, 
                    cuda_devices=[gpu_id],
                    context_length=4096,
                    **kwargs)
            else:
                runtime = ExtendedSGLangRuntime(
                    model_path=model_path,
                    cuda_devices=[gpu_id],
                    gpu=gpu_id,
                    context_length=4096,
                    **kwargs,
                )
            self.runtimes.append(runtime)

        # parallelizae loading for each gpu
        for config in gpu_configs:
            load_runtime(config)

    def select_runtime_with_identifiers(self, text, sampling_params, input_ids) -> EndpointRuntimeInterface:
        experiment_id = sampling_params.pop("experiment_id", random_uuid_string())
        request_id = sampling_params.pop("request_id", random_uuid_string())
        runtime_id = self.request_router.select_runtime(text, experiment_id, request_id, input_ids)
        return self.runtimes[runtime_id]

    def async_wrap(f):
        async def _func(*args, **kwargs):
            return f(*args, **kwargs)

        return _func
    
    @async_wrap
    def async_select_runtime_with_identifiers(self, text, sampling_params, input_ids) -> EndpointRuntimeInterface:
        return self.select_runtime_with_identifiers(text, sampling_params, input_ids)

    def generate_request(self, text, sampling_params):
        runtime: EndpointRuntimeInterface = (
            self.select_runtime_with_identifiers(text, sampling_params)
        )
        start_time = time.time()
        output =  requests.post(
            runtime.generate_url,
            json={
                "text": text,
                "sampling_params": sampling_params,
            },
            timeout=60 * 10,
        ).json()
        output["request_latency"] = time.time() - start_time
        return output
    
    def generate_batch_request(self, batch_kwargs, sampling_params, num_threads):
        with ThreadPoolExecutor(num_threads) as executor:
            futures = []
            for arguments in batch_kwargs:
                futures.append(
                    executor.submit(
                        self.generate_request, arguments, sampling_params
                    )
                )
            rets = [f.result() for f in futures]
            return rets

    def update_runtime_selection_policy(self, runtime_selection_policy, custom_runtime_selector=None):
        self.request_router.update_runtime_selection_policy(runtime_selection_policy)
        self.request_router.custom_selector = custom_runtime_selector

    def clear_kv_cache(self):
        for runtime in self.runtimes:
            requests.get(runtime.flush_cache_url)

    async def async_generate_batch_request_per_sec(
        self,
        requests: Iterable,
        request_rate: float,
        routine,
    ):
        self.current_experiment_state_time = time.time()
        async def get_request(
            input_requests,
            request_rate: float,
        ):
            input_requests = iter(input_requests)
            for request in input_requests:
                yield request
                if request_rate == float("inf"):
                    continue
                interval = np.random.exponential(1.0 / request_rate)
                await asyncio.sleep(interval)
        if self.start_time is None:
            self.start_time = time.time()
        tasks: List[asyncio.Task] = []
        async for request in get_request(requests, request_rate):
            task = asyncio.create_task(routine(**request))
            tasks.append(task)
        results = await asyncio.gather(*tasks)
        return results

    async def async_send_request(
        self, text=None, sampling_params=None, input_ids=None
    ): 
        start_time = time.time()
        rid = random_uuid_string()
        sampling_params["request_id"] = rid
        # runtime: EndpointRuntimeInterface = (
        #     self.select_runtime_with_identifiers(text, sampling_params)
        # )
        runtime = await asyncio.to_thread(
            self.select_runtime_with_identifiers, text, sampling_params, input_ids
        )
        # runtime = await self.async_select_runtime_with_identifiers(text, sampling_params)
        timeout = aiohttp.ClientTimeout(total=3 * 3600)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            self.request_sent_time.append(time.time() - self.start_time)
            while True:
                async with session.post(runtime.generate_url,
                    json={
                        "text": text,
                        "sampling_params": sampling_params,
                        "rid": rid,
                    },) as response:
                    chunks = []
                    ttft = 0
                    async for chunk, _ in response.content.iter_chunks():
                        if ttft == 0:
                            ttft = time.time() - start_time
                        chunks.append(chunk)
                    request_latency = time.time() - start_time
                    global_time = time.time() - self.current_experiment_state_time
                output = b"".join(chunks).decode("utf-8")
                output = json.loads(output)
                # Re-send the request if it failed.
                if "error" not in output:
                    break
        output["request_latency"] = request_latency
        output["TTFT"] = ttft
        output["global_time"] = global_time
        output["topt_req_sec"] = output["meta_info"]["completion_tokens"] / request_latency
        output["total_tokens"] = output["meta_info"]["prompt_tokens"] + output["meta_info"]["completion_tokens"]
        #  throughput as token generated per second
        # print(f"{id} finishes")
        return output
    

