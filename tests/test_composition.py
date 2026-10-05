from pathlib import Path
from types import SimpleNamespace
import pytest
from mncs_env import composition, doctor
from mncs_env.capabilities import probe_availability


def binding(path):
    return {'capability':'producer:compile','binding_id':'bound-exact', 'provider':'producer',
            'contract_revision':'1','toolchain_address':str(path),'provenance':{'checkout':{'path':str(path.parent),'head':'selected-head'},'artifact_contract':{'artifact_schema':'frozen/1'}}}


def test_roles_are_provider_references_and_byte_observations(tmp_path):
    producer=tmp_path/'producer'; producer.write_bytes(b'producer-one')
    reference=tmp_path/'reference'; reference.write_bytes(b'oracle')
    selectors={'compiler':{'capability':'producer:compile'},'reference':{'capability':'producer:compile','use_reference_toolchain':True}}
    first=composition.resolve(selectors,[binding(producer)],{'binary':str(reference)})
    assert first['compatibility']['state']=='unproven'
    assert first['roles']['compiler']['binding_id']=='bound-exact'
    assert first['roles']['compiler']['executable'] != first['roles']['reference']['executable']
    producer.write_bytes(b'producer-two')
    second=composition.resolve(selectors,[binding(producer)],{'binary':str(reference)},first)
    assert first['roles']['compiler']['executable'] != second['roles']['compiler']['executable']
    assert first['roles']['reference']==second['roles']['reference']


def test_provider_readiness_is_joined_only_to_current_role_identity(tmp_path):
    producer=tmp_path/'producer'; producer.write_bytes(b'producer-one')
    selectors={'compiler':{'capability':'producer:compile'}}
    service='doctor:compiler-vm-coherence'
    first=composition.resolve(selectors,[binding(producer)],None,
                              compatibility_service=service)
    assert first['compatibility']['state']=='unproven'
    evidence=[{'identity':service,'status':'ready','observed_at':'now',
               'composition_identity':first['identity']}]
    verified=composition.resolve(selectors,[binding(producer)],None,first,
                                 compatibility_service=service,
                                 service_observations=evidence)
    assert verified['compatibility']['state']=='verified'
    assert verified['compatibility']['evidence_identity']==service
    producer.write_bytes(b'producer-two')
    stale=composition.resolve(selectors,[binding(producer)],None,verified,
                              compatibility_service=service,
                              service_observations=evidence)
    assert stale['compatibility']['state']=='unproven'
    assert stale['compatibility']['evidence_stale'] is True


def test_doctor_build_receipt_is_joined_to_selected_checkout_and_bytes(tmp_path):
    import hashlib

    producer=tmp_path/'producer'; producer.write_bytes(b'producer-one')
    selectors={'compiler':{'capability':'producer:compile'}}
    service='doctor:compiler-vm-coherence'
    first=composition.resolve(selectors,[binding(producer)],None,
                              compatibility_service=service)
    component={
        'state':'ready', 'checkout':str(producer.parent),
        'executable':str(producer),
        'executable_sha256':hashlib.sha256(producer.read_bytes()).hexdigest(),
        'build_origin':'locally-observed-compiled-inputs',
        'producer':{'identity':'sha256:producer-receipt',
                    'receipt':{'source_revision':'selected-head',
                               'stage0_revision':'stage0-pin',
                               'assurance':'provider-owned local build receipt'}},
        'mismatches':[],
    }
    evidence=[{'identity':service,'status':'ready','observed_at':'now',
               'response_schema':'mncs.doctor.compiler-vm/1',
               'provider_components':{'compiler':component},
               'composition_identity':first['identity']}]
    verified=composition.resolve(selectors,[binding(producer)],None,first,
                                 compatibility_service=service,
                                 service_observations=evidence)
    assert verified['compatibility']['state']=='verified'
    origin=verified['roles']['compiler']['provider_build_origin']
    assert origin['state']=='matches-selected-inputs'
    assert origin['receipt_identity']=='sha256:producer-receipt'
    assert origin['dependency_revision']=='stage0-pin'

    producer.write_bytes(b'replaced executable')
    changed=composition.resolve(selectors,[binding(producer)],None,verified,
                                compatibility_service=service)
    evidence[0]['composition_identity']=changed['identity']
    mismatched=composition.resolve(selectors,[binding(producer)],None,changed,
                                   compatibility_service=service,
                                   service_observations=evidence)
    assert mismatched['compatibility']['state']=='unproven'
    assert mismatched['compatibility']['provider_build_evidence']['compiler']=='mismatch'


def test_compatibility_service_must_be_declared():
    with pytest.raises(ValueError,match='reference a declared service'):
        composition.validate_compatibility_service(
            'doctor:compiler-vm-coherence', [{'identity':'doctor:other'}])


def test_missing_role_fails_closed():
    value=composition.resolve({'runtime':{'capability':'vm:runtime'}},[],None)
    assert value['roles']['runtime']['state']=='unbound'
    with pytest.raises(ValueError): composition.validate({'compiler':{'command':'ambient'}})


def test_selected_role_without_checkout_provenance_is_unproven(tmp_path):
    executable=tmp_path/'compiler'; executable.write_bytes(b'compiler')
    selected_binding={
        'capability':'compiler:artifact', 'binding_id':'compiler-binding',
        'provider':'mncs-compiler', 'toolchain_address':str(executable),
    }
    drift=composition.selected_checkout_drift(
        {'compiler':{'capability':'compiler:artifact'}}, [selected_binding], {}, tmp_path)
    assert drift == [{
        'provider':'mncs-compiler', 'path':None,
        'reasons':['provider-checkout-unselected'], 'selected':None, 'observed':None,
    }]


def test_selected_binary_replacement_invalidates_epoch_without_git(tmp_path):
    binary=tmp_path/'vm';binary.write_bytes(b'old')
    session=SimpleNamespace(snapshot={'bindings':[{'fixed_env':{'MNCS_VM_BIN':str(binary), 'MNCS_VM_CHECKOUT':str(tmp_path)}}]})
    old=doctor._execution_stamps(session)
    binary.write_bytes(b'new')
    assert doctor._execution_stamps(session)!=old
    binary.unlink()
    assert doctor._execution_stamps(session)[str(binary)] is None


def test_disappearing_composed_substrate_is_unavailable(tmp_path):
    script=tmp_path/'provider.py';script.write_text('print(1)')
    executable=tmp_path/'missing-vm'
    value=probe_availability({'address':'python:'+str(script),'fixed_env':{'MNCS_VM_BIN':str(executable)}})
    assert value['availability']['status']=='unavailable'
    assert value['availability']['code']=='toolchain-missing'


def test_normal_entry_resume_reobserves_selected_runtime_bytes(tmp_path):
    import json
    import subprocess
    from mncs_env.entry import enter
    from mncs_env.sessions import Session
    roots={}
    for name in ('mncs-language','producer','runtime'):
        root=tmp_path/name;root.mkdir();roots[name]=root
        subprocess.run(['git','init','-q','-b','main',str(root)],check=True)
        executable=root/'target/debug/tool';executable.parent.mkdir(parents=True)
        response = '{"schema_version":"test.readiness/1","status":"pass"}' if name == 'runtime' else '{}'
        executable.write_text(f'#!/bin/sh\nprintf \'%s\\n\' \'{response}\'\n');executable.chmod(0o755)
        if name=='mncs-language':
            reference=executable.parent/'mncs';reference.write_bytes(executable.read_bytes());reference.chmod(0o755)
        meta=root/'.mncs';meta.mkdir()
        contract={'mncs-language':'cli-toolchain','producer':'compiler','runtime':'vm'}[name]
        provides=[{'contract':contract,'version':'1','effects':['read'],'invocation':{'kind':'executable','path':'target/debug/tool'}}]
        if name == 'runtime':
            provides.append({'contract':'coherence','version':'1','effects':['read'],
                             'invocation':{'kind':'executable','path':'target/debug/tool'}})
        (meta/'project.json').write_text(json.dumps({'repository':name,'revision':1,'contracts':{'provides':provides,'consumes':[]}}))
        subprocess.run(['git','-C',str(root),'add','.'],check=True)
        subprocess.run(['git','-C',str(root),'-c','user.name=Proof','-c','user.email=proof@example.invalid','commit','-qm','isolated provider fixture'],check=True)
    definition={'name':'isolated composition proof','workspace_root':str(tmp_path),'workspace_scope':{'kind':'workspace','repositories':list(roots)},'intent':{'goal':'readonly selected byte reconciliation'},'execution_roles':{'reference':{'capability':'mncs-language:cli-toolchain','use_reference_toolchain':True},'compiler':{'capability':'producer:compiler'},'runtime':{'capability':'runtime:vm'}},'execution_compatibility_service':'runtime:coherence','services':[{'identity':'runtime:coherence','probe':{'capability':'runtime:coherence','argv':[]},'response_schema':'test.readiness/1','ready_when':{'/status':'pass'},'required':True}]}
    options=dict(definition=definition,definition_path=None,workspace_root=str(tmp_path),state_dir=tmp_path/'state',backend='file',consumer_id='composition-proof',consumer_kind='agent')
    first=enter(**options,new_session=True)
    assert first['execution_stack']['compatibility']['state']=='verified'
    prior=first['execution_stack']['roles']['runtime']['executable']['artifact_identity']
    selected=roots['runtime']/'target/debug/tool';selected.write_text(selected.read_text()+'# controlled byte change\n')
    second=enter(**options)
    assert second['session_id']==first['session_id'] and second['entry']['reused']
    assert second['execution_stack']['roles']['runtime']['executable']['artifact_identity'] != prior
    assert second['execution_stack']['compatibility']['state']=='verified'
    assert second['execution_stack']['compatibility']['service_identity']=='runtime:coherence'
    assert second['execution_stack']['roles']['reference']['executable']['artifact_identity']==first['execution_stack']['roles']['reference']['executable']['artifact_identity']
    resumed=Session.resume(state_dir=tmp_path/'state',session_id=first['session_id'],backend='file')
    try:assert resumed.context()['execution_stack']==second['execution_stack']
    finally:resumed.close()
