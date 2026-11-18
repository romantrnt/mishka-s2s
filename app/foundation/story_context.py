"""Explicit deployment receipt, not automatic certification of model coverage."""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
import json
import math
import re
import time
from urllib.parse import urlparse
import httpx

@dataclass(frozen=True)
class StoryContextReceipt:
    base_url: str
    model: str
    model_digest: str
    server_version: str
    context_ceiling: int
    template_reserve: int
    expires_at: float
    # Resource estimate only. Backend no-truncation enforcement and clean stop,
    # not a bytes-to-tokens theorem, enforce fail-closed context handling.
    runner: str = 'llamacpp'
    tokenizer_contract: str = 'utf8_byte_budget_v1'
    truncate_false_verified: bool = False
    shift_false_verified: bool = False

class StoryContextError(RuntimeError):
    pass

class StoryBackendContract:
    def __init__(self, receipt: StoryContextReceipt, *, timeout_s: float = 2.0):
        self.receipt=receipt
        if type(timeout_s) not in (int,float) or not 0<timeout_s<=5:
            raise StoryContextError("story_backend_timeout")
        self.timeout_s=timeout_s
        self._observed=None
        self._refresh_generation=0
        self._http_slots=asyncio.Semaphore(2)

    def matches(self, base_url, model):
        r=self.receipt
        return (r.base_url.rstrip('/')==base_url.rstrip('/') and r.model==model
                and type(r.expires_at) in (int,float) and math.isfinite(r.expires_at) and r.expires_at>time.time()
                and r.runner=='llamacpp'
                and r.tokenizer_contract=='utf8_byte_budget_v1'
                and r.truncate_false_verified is True and r.shift_false_verified is True
                and type(r.context_ceiling) is int and 4096<=r.context_ceiling<=32768
                and type(r.template_reserve) is int and 256<=r.template_reserve<=4096
                and isinstance(r.model_digest,str) and bool(re.fullmatch(r'(?:sha256:)?[0-9a-f]{64}',r.model_digest))
                and isinstance(r.server_version,str) and bool(re.fullmatch(r'v?\d+\.\d+\.\d+(?:[-+].+)?',r.server_version)))

    def ready(self, base_url, model):
        return self.matches(base_url,model) and self._observed==(self.receipt.model_digest,self.receipt.server_version)

    def budget(self,messages,output_tokens,base_context):
        r=self.receipt
        if not self.ready(r.base_url,r.model):raise StoryContextError('story_backend_unverified')
        if type(output_tokens) is not int or not 0<output_tokens<=6000:raise StoryContextError('story_context_budget')
        if type(base_context) is not int or not 0<base_context<=r.context_ceiling:raise StoryContextError('story_context_budget')
        try:
            size=sum(len(item['content'].encode('utf-8')) for item in messages)
        except (KeyError,TypeError,UnicodeError):raise StoryContextError('story_context_budget') from None
        needed=max(base_context,((size+r.template_reserve+output_tokens+255)//256)*256)
        if needed>r.context_ceiling:raise StoryContextError('story_context_budget')
        return needed

    MAX_METADATA_BYTES = 1024 * 1024

    async def _metadata(self,path):
        # No caller-provided destination, credentials, profile or prompt is sent.
        parsed=urlparse(self.receipt.base_url)
        if (parsed.scheme!='http' or parsed.hostname not in {'localhost','127.0.0.1','::1'}
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {'','/'}):
            raise StoryContextError('story_backend_endpoint')
        # The receipt permits a direct local endpoint, never an environment proxy.
        async with httpx.AsyncClient(timeout=self.timeout_s, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", self.receipt.base_url.rstrip('/')+path,
                                     headers={"Accept-Encoding": "identity"}) as response:
                response.raise_for_status()
                if int(response.headers.get("content-length", "0")) > self.MAX_METADATA_BYTES:
                    raise StoryContextError('story_backend_metadata')
                raw=bytearray()
                async for chunk in response.aiter_raw():
                    if len(raw)+len(chunk)>self.MAX_METADATA_BYTES:
                        raise StoryContextError('story_backend_metadata')
                    raw.extend(chunk)
        return json.loads(raw)

    async def refresh(self):
        r=self.receipt
        self._refresh_generation+=1
        generation=self._refresh_generation
        if not self.matches(r.base_url,r.model):
            self._observed=None
            raise StoryContextError('story_backend_unverified')
        try:
            # Queueing and both metadata calls share one cancellable deadline.
            async with asyncio.timeout(self.timeout_s):
                async with self._http_slots:
                    version=await self._metadata('/api/version')
                    running=await self._metadata('/api/ps')
            if self.receipt is not r:
                raise StoryContextError('story_backend_changed')
            row=next((m for m in running.get('models',[]) if m.get('name')==r.model or m.get('model')==r.model),None)
            if (version.get('version')!=r.server_version or row is None
                    or row.get('digest')!=r.model_digest
                    or row.get('runner')!=r.runner
                    or type(row.get('context_length')) is not int
                    or not 0<row['context_length']<=r.context_ceiling):
                raise StoryContextError('story_backend_changed')
            if not self.matches(r.base_url,r.model):raise StoryContextError('story_backend_unverified')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Any observed failure revokes availability and all in-flight
            # successes; an older successful check cannot restore it later.
            self._observed=None
            self._refresh_generation+=1
            if isinstance(exc,StoryContextError):raise
            raise StoryContextError('story_backend_metadata') from None
        if generation!=self._refresh_generation:
            raise StoryContextError('story_backend_changed')
        # Publish a complete verification atomically. Cancellation is not
        # a new observation; providers still refresh before every request.
        self._observed=(r.model_digest,r.server_version)


def load_story_contracts(path, *, base_url, generator_model, validator_model):
    """Read an operator-owned local JSON document; never accept request data."""
    if not path:return {}
    import os
    import stat
    from dataclasses import fields
    def strict_pairs(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('duplicate')
            result[key]=value
        return result
    try:
        if not isinstance(path,str) or not os.path.isabs(path):return {}
        fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as stream:
            mode=os.fstat(stream.fileno())
            if not stat.S_ISREG(mode.st_mode) or mode.st_size>16384:return {}
            raw=stream.read(16385)
        if len(raw)>16384:return {}
        data=json.loads(raw,object_pairs_hook=strict_pairs)
        if not isinstance(data,dict) or set(data)!={'generator','validator'}:return {}
        result={};expected={f.name for f in fields(StoryContextReceipt)}
        for role,model in (('generator',generator_model),('validator',validator_model)):
            value=data[role]
            if not isinstance(value,dict) or set(value)!=expected:return {}
            receipt=StoryContextReceipt(**value)
            contract=StoryBackendContract(receipt)
            if not contract.matches(base_url,model):return {}
            # Validate endpoint without issuing any request.
            parsed=urlparse(receipt.base_url)
            if (parsed.scheme!='http' or parsed.hostname not in {'localhost','127.0.0.1','::1'}
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {'','/'}):return {}
            result[role]=contract
        return result
    except (OSError,ValueError,TypeError,AttributeError,OverflowError):return {}

async def refresh_story_contracts(engine):
    """Controlled startup refresh: only bounded local metadata GETs."""
    contracts=getattr(engine,'story_backend_contracts',{})
    async def one(contract):
        try:
            await contract.refresh()
            return True
        except StoryContextError:return False
    results=await asyncio.gather(*(one(c) for c in contracts.values()))
    return bool(results) and all(results)
