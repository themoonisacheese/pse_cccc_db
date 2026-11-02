"""Provider request isolation and response safety, through the actual worker."""
import asyncio
import json
from types import SimpleNamespace
import httpx
import pytest
from app.services.ingest import llm_worker as mod


def test_hyper_payload_unchanged():
    p=mod._completion_payload(base_url='https://hyper.charm.land/v1',model='deepseek-v4-flash-0731',user_prompt='input')
    assert p=={'model':'deepseek-v4-flash-0731','messages':[{'role':'system','content':mod.SYSTEM_PROMPT},{'role':'user','content':'input'}],'temperature':0.0,'max_tokens':60,'thinking':{'type':'disabled'}}


def test_openrouter_nano_disables_reasoning_and_pins_openai():
    p=mod._completion_payload(base_url='https://openrouter.ai/api/v1/',model='openai/gpt-5.4-nano',user_prompt='input')
    assert p['provider']=={'only':['openai'],'allow_fallbacks':False}
    assert p['reasoning']=={'effort':'none'} and p['usage']=={'include':True}
    assert 'temperature' not in p and 'thinking' not in p and p['max_tokens']==60


def test_openrouter_non_openai_is_not_pinned_to_openai():
    p=mod._completion_payload(base_url='https://openrouter.ai/api/v1',model='other/model',user_prompt='input')
    assert 'provider' not in p and 'thinking' not in p and 'reasoning' not in p
    assert p['temperature']==0.0


def test_old_openai_nano_keeps_temperature_without_reasoning():
    p=mod._completion_payload(base_url='https://openrouter.ai/api/v1',model='openai/gpt-4.1-nano',user_prompt='input')
    assert p['temperature']==0.0 and 'reasoning' not in p


def test_provider_detection_uses_hostname():
    p=mod._completion_payload(base_url='https://openrouter.ai.example/v1',model='openai/gpt-5.4-nano',user_prompt='input')
    assert 'provider' not in p and p['thinking']=={'type':'disabled'}


@pytest.mark.parametrize(('finish','content','expected'),[('stop',' familiar ','familiar'),('stop','',None),('stop',None,None),('length','fami',None),('content_filter','familiar',None)])
def test_actual_worker_http_path(monkeypatch,finish,content,expected):
    sent=[]
    settings=SimpleNamespace(llm_base_url='https://openrouter.ai/api/v1',llm_model='openai/gpt-5.4-nano',llm_api_key='test-key')
    monkeypatch.setattr(mod,'get_settings',lambda:settings)
    def handler(request):
        sent.append(json.loads(request.content));assert request.headers['authorization']=='Bearer test-key'
        return httpx.Response(200,json={'choices':[{'finish_reason':finish,'message':{'content':content}}]})
    client_type=httpx.AsyncClient
    monkeypatch.setattr(mod.httpx,'AsyncClient',lambda **kwargs:client_type(transport=httpx.MockTransport(handler),**kwargs))
    async def call():return await mod.LlmWorker()._call_llm(clue_text='clue (8)',content='f ami liar',enumeration='(8)')
    if expected is None:
        with pytest.raises(mod._LlmUnavailable):asyncio.run(call())
    else:assert asyncio.run(call())==expected
    assert len(sent)==1 and sent[0]['reasoning']=={'effort':'none'}
    assert sent[0]['messages'][1]['content'].endswith('What is the answer?')
