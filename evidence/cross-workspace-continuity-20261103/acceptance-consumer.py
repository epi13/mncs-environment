import json, subprocess, sys
from pathlib import Path
PYTHON='/home/epi13/Documents/Projects/mncs-control-mcp/.venv/bin/python'
EVIDENCE=Path('/tmp/cross-workspace-mcp-acceptance-20261103.json')
def call(label,request,success=True):
    r=subprocess.run([PYTHON,'-B','-m','mncs_control_mcp.environment_mcp_client'],input=json.dumps(request),text=True,capture_output=True,timeout=420)
    payload=json.loads(r.stdout)
    structured=payload.get('structuredContent',payload)
    rows=json.loads(EVIDENCE.read_text()) if EVIDENCE.exists() else []
    rows.append({'label':label,'request':request,'response':structured,'process_exit_code':r.returncode})
    EVIDENCE.write_text(json.dumps(rows,indent=2)+'\n')
    print(json.dumps({'label':label,'ok':structured.get('ok'),'exit_code':structured.get('exit_code'),'result':structured.get('result',structured.get('error'))},indent=2),flush=True)
    if success and (r.returncode or structured.get('ok') is not True or structured.get('exit_code',0) != 0):raise RuntimeError(label)
    return structured
if __name__=='__main__':call(sys.argv[1],json.loads(sys.argv[2]),success='--expect-rejection' not in sys.argv)
