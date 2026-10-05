#!/usr/bin/env python3
"""Exercise provider composition in an already entered, explicitly claimed session.

This is proof choreography, not Environment compiler/runtime implementation.
All source compilation, admission, Test policy, debug and health operations go
through normal bound capabilities. No PATH compiler lookup is used.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mncs_env.sessions import Session


def write(path,value):
    path.write_text(json.dumps(value,sort_keys=True,indent=2)+'\n');return str(path)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session',required=True);parser.add_argument('--state-dir',required=True)
    parser.add_argument('--output',required=True);args=parser.parse_args()
    output=Path(args.output).resolve();output.mkdir(parents=True,exist_ok=True)
    started=time.monotonic();s=Session.resume(state_dir=args.state_dir,session_id=args.session,backend='store')
    timings={};invocations=[]
    def invoke(cap,argv,name):
        t=time.monotonic();result=s.invoke(cap,argv,timeout_seconds=180,output_limit_bytes=2097152)
        timings[name]=round(time.monotonic()-t,6);write(output/(name+'-invocation.json'),result)
        invocations.append({'capability':cap,'status':result['status'],'returncode':result['returncode']})
        if result['returncode'] != 0 or result.get('truncated'):raise RuntimeError(f'{cap} refused: {result}')
        value=json.loads(result['stdout']);write(output/(name+'.json'),value);return value
    try:
        s.revalidate();before=s.context();write(output/'context.json',before)
        checkouts={name:Path(row['path']) for name,row in s.snapshot['selected_checkouts'].items()}
        source=checkouts['mncs-vm']/'tests/corpus/arith.mncs'
        integer=lambda value:{'integer':{'value':value,'type':{'bits':64,'signed':True}}}
        call={'schema_version':'0.1','target':{'module':'mncs.vmcorpus.arith.v1','function':'add3'},'arguments':[integer(10)],'step_budget':1000}
        call_path=write(output/'call.json',call)
        request={'schema_version':'mncs.compiler-vm-request/1','source':str(source),'logical_name':'arith.mncs','calls':[call]}
        request_path=write(output/'compiler-request.json',request);cache=output/'shared'
        producer=invoke('mncs-compiler:compiler-producer',[],'producer')
        runtime=invoke('mncs-vm:vm-runtime',[],'runtime')
        product=invoke('mncs-compiler:canonical-vm-artifact',['--request',request_path,'--cache',str(cache)],'emission')
        warm=invoke('mncs-compiler:canonical-vm-artifact',['--request',request_path,'--cache',str(cache)],'warm-emission')
        assert warm['cache_reused'] and product['artifact']==warm['artifact']
        artifact=cache/product['artifact']['address'];evidence=json.loads((cache/product['evidence']['address']).read_text())
        admission=invoke('mncs-vm:vm-admission',['--artifact',str(artifact)],'admission')
        execution=invoke('mncs-vm:vm-call',['--artifact',str(artifact),'--request',call_path],'execution')
        oracle=evidence['reference_results'][0]
        assert execution['execution']['returned']==oracle['returned'] and execution['outcome']['kind']=='completed'
        assert admission['artifact_id']==product['artifact']['identity']==execution['record']['artifact_id']
        assert next(r['limit'] for r in execution['record']['resource_limits'] if r['dimension']=='steps')==call['step_budget']
        doctor=invoke('mncs-doctor:compiler-vm-health',['--product',product['product'],'--request',call_path],'doctor')
        assert doctor['status']=='pass'
        tests=invoke('mncs-test:canonical-vm-tests',['--manifest',str(checkouts['mncs-test']/'mncs-test.toml'),'--library',str(checkouts['mncs-test'].parent/'mncs-stdlib/library'),'--vm-cache',str(cache),'--artifacts',str(output/'test-artifacts'),'--result',str(output/'test-result.json'),'--check-result',str(output/'test-check.json'),'--format','json'],'test')
        # A real Forge native kernel, with two isolated calls in one process.
        forge_call={'schema_version':'0.1','target':{'module':'mncs.forge.core.v1','function':'lifecycle_initial'},'arguments':[],'step_budget':100000}
        forge_calls=write(output/'forge-calls.json',[forge_call,forge_call]);native=checkouts['mncs-forge']/'src/mncs_forge/resources/native'
        forge=invoke('mncs-forge:canonical-vm-work',['--source',str(native/'forge/core.mncs'),'--calls',forge_calls,'--cache',str(cache),'--library',str(native),'--library',str(checkouts['mncs-forge'].parent/'mncs-stdlib/library')],'forge')
        assert forge['retained']['calls']==2 and forge['retained']['cache_reused']
        assert forge['results'][0]['execution']['returned']==forge['results'][1]['execution']['returned']
        operations=[r for r in evidence['source_map']['operations'] if r['correspondence'].startswith('selected-ssa') and '::add3' in r['function_identity']]
        debug_root=output/'debug';live_started=False
        try:
            debug=invoke('mncs-debug:canonical-vm-debug',['start','--root',str(debug_root),'--compiler-product',product['product'],'--callable','mncs.vmcorpus.arith.v1::add3','--args-json',json.dumps(call['arguments']),'--stop-op',operations[0]['identity'],'--capture','bounded'],'debug-start');live_started=True
            assert debug['event']['event']=='stopped' and debug['event']['stop']['safe_point']['instruction']==operations[0]['identity']
            invoke('mncs-debug:canonical-vm-debug',['inspect','--root',str(debug_root)],'debug-inspect')
            step=invoke('mncs-debug:canonical-vm-debug',['step-in','--root',str(debug_root)],'debug-step');assert step['event']['event']=='stopped'
            resumed=invoke('mncs-debug:canonical-vm-debug',['resume','--root',str(debug_root)],'debug-resume');assert resumed['event']['event']=='finished'
            assert resumed['event']['record']['artifact_id']==product['artifact']['identity']
        finally:
            if live_started:invoke('mncs-debug:canonical-vm-debug',['close','--root',str(debug_root)],'debug-close')
        digest=hashlib.sha256(json.dumps(oracle['returned'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
        checkpoint=s.checkpoint(progress='normal Environment-bound compiler/VM/Test/Debug/Doctor/Forge proof passed',remaining=['delivery and final re-entry'])
        report={'schema_version':'mncs.environment.compiler-vm-proof/1','session':s.session_id,'checkpoint':checkpoint['identity'],'composition':before['execution_stack'],'producer':producer,'runtime':runtime,'artifact':product['artifact'],'build_receipt':product['build_receipt'],'semantic_return_digest':digest,'reference_agreement':True,'resource_steps_limit':call['step_budget'],'cache_reused':warm['cache_reused'],'forge_retained_calls':forge['retained']['calls'],'debug_operation':operations[0],'doctor_status':doctor['status'],'invocations':invocations,'timings_seconds':timings,'elapsed_seconds':round(time.monotonic()-started,3)}
        write(output/'proof.json',report);print(json.dumps({'status':'pass','session':s.session_id,'checkpoint':checkpoint['identity'],'evidence':str(output/'proof.json'),'timings_seconds':timings},indent=2))
    finally:s.close()

if __name__=='__main__':main()
