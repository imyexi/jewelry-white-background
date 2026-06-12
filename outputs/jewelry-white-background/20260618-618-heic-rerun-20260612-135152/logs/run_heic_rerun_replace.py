import csv
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from PIL import Image

CWD = Path(r"C:\Users\Administrator\Documents\珠宝白底图生成")
BASE_TOKEN = "D4Vjbv19WaVVTwsGKdJcsnt5neg"
TABLE_ID = "tblwSMbqUjjJ3Eiy"
PRODUCT_IMAGE_FIELD = "fldWCFdZbJ"
PYTHON = Path(r"C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe")
HELPER_PATH = Path(r"C:\Users\Administrator\yuan-image\.agents\skills\aireiter-image-generation\scripts\aireiter_image_helper.py")
WATERMARK_SCRIPT = Path(r"C:\Users\Administrator\.codex\skills\yuanyuan-ruyi-watermark\scripts\watermark_images.py")
PROMPT_PATH = Path(r"C:\Users\Administrator\.codex\skills\jewelry-white-background\references\white-background-prompt.md")
LARK_CLI = r"C:\Users\Administrator\AppData\Roaming\npm\lark-cli.cmd"
RUN_ROOT = Path(sys.argv[1]).resolve()
BATCH_SIZE = int(sys.argv[2]) if len(sys.argv) > 2 else 10
POLL_INTERVAL = 8
BATCH_TIMEOUT = 1800

spec = importlib.util.spec_from_file_location("aireiter_helper", str(HELPER_PATH))
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
API_KEY = helper.env_api_key()
PROMPT = PROMPT_PATH.read_text(encoding="utf-8")

STATE_PATH = RUN_ROOT / "logs" / "heic_rerun_state.json"
EVENTS_PATH = RUN_ROOT / "logs" / "heic_rerun_events.jsonl"
UPLOAD_MANIFEST = RUN_ROOT / "logs" / "heic_replacement_manifest.jsonl"
QC_PATH = RUN_ROOT / "logs" / "heic_basic_qc.jsonl"
REGISTRY_PATH = CWD / "outputs" / "jewelry-white-background" / "generated_products_registry.csv"
REGISTRY_FIELDS = ["product_id","record_id","settlement","source_file_token","task_id","source_path","generated_path","final_path","upload_status","uploaded_file_token","uploaded_at","run_root"]

for sub in ["generated", "white-bg", "logs"]:
    (RUN_ROOT / sub).mkdir(parents=True, exist_ok=True)

def safe_name(text):
    return re.sub(r"[\\/:*?\"<>|]+", "_", str(text or "").strip()) or "unnamed"

def rel(path):
    return os.path.relpath(str(Path(path).resolve()), str(CWD))

def run_cmd(args, timeout=300):
    proc=subprocess.run(args,cwd=str(CWD),text=True,encoding="utf-8",errors="replace",stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=timeout)
    return proc.returncode, proc.stdout

def load_state():
    if STATE_PATH.exists(): return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}

def save_state(state):
    tmp=STATE_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
    tmp.replace(STATE_PATH)

def event(kind,row):
    payload={"ts":time.strftime("%Y-%m-%dT%H:%M:%S"),"kind":kind,**row}
    with EVENTS_PATH.open('a',encoding='utf-8') as f: f.write(json.dumps(payload,ensure_ascii=False,separators=(',',':'))+'\n')
    print(f"[{payload['ts']}] {kind}: {row.get('product_id','')} {row.get('status','')}",flush=True)

def append_registry(row):
    exists=REGISTRY_PATH.exists()
    with REGISTRY_PATH.open('a',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=REGISTRY_FIELDS)
        if not exists: w.writeheader()
        w.writerow({k:row.get(k,'') for k in REGISTRY_FIELDS})

def submit_task(item):
    pn=safe_name(item['product_id'])
    task_id=f"jewelry-white-bg-heicfix-{pn}-{time.strftime('%Y%m%d%H%M%S')}"
    image_value=helper.normalize_image_input(item['prepared_path'])
    payload={"model":"gpt_image_2","params":{"prompt":PROMPT,"aspect_ratio":"3:4","resolution":"2K","image_url":image_value},"out_task_id":task_id}
    result=helper.post_json(helper.SUBMIT_URL,payload,API_KEY)
    (RUN_ROOT/'logs'/f'{pn}_submit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    if not helper.is_submit_accepted(result): raise RuntimeError(json.dumps(result,ensure_ascii=False))
    return task_id

def query_task(task_id):
    return helper.post_json(helper.QUERY_URL,{"out_task_id":task_id},API_KEY)

def download_generated(pn,url):
    out=RUN_ROOT/'generated'/f'{safe_name(pn)}_generated.png'
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0","Accept":"image/*,*/*"})
    with urllib.request.urlopen(req,timeout=180) as resp: out.write_bytes(resp.read())
    return out

def run_watermark(generated):
    queue=RUN_ROOT/'logs'/f'watermark_queue_{int(time.time())}.csv'
    with queue.open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['image_path','product_id','output_path']); w.writeheader()
        for g in generated:
            pn=g['product_id']
            w.writerow({'image_path':g['generated_path'],'product_id':pn,'output_path':str(RUN_ROOT/'white-bg'/f'{safe_name(pn)}_white_bg_watermarked.png')})
    code,raw=run_cmd([str(PYTHON),str(WATERMARK_SCRIPT),'--queue',str(queue),'--output-dir',str(RUN_ROOT/'white-bg')],timeout=600)
    (RUN_ROOT/'logs'/f'watermark_{int(time.time())}.log').write_text(raw,encoding='utf-8')
    if code!=0: raise RuntimeError(raw)

def basic_qc(path):
    with Image.open(path) as im: w,h=im.size
    ratio=w/h; size=Path(path).stat().st_size
    return {"ok":abs(ratio-0.75)<0.02 and size>100000,"width":w,"height":h,"ratio":round(ratio,4),"size":size}

def parse_json_from_raw(raw):
    idx=raw.find('{')
    if idx<0: return None
    return json.loads(raw[idx:])

def upload_new(item,final_path):
    code,raw=run_cmd([LARK_CLI,'base','+record-upload-attachment','--as','user','--base-token',BASE_TOKEN,'--table-id',TABLE_ID,'--record-id',item['record_id'],'--field-id',PRODUCT_IMAGE_FIELD,'--file',rel(final_path)],timeout=300)
    token=''
    if code==0:
        obj=parse_json_from_raw(raw)
        attachments=obj['data']['attachments'][item['record_id']][PRODUCT_IMAGE_FIELD]
        expected=Path(final_path).name
        for att in attachments:
            if att.get('name')==expected: token=att.get('file_token','')
    if code!=0: raise RuntimeError(raw)
    return token, raw

def remove_old(item):
    old=item.get('old_uploaded_file_token') or ''
    if not old: return '', 'no old token'
    code,raw=run_cmd([LARK_CLI,'base','+record-remove-attachment','--as','user','--base-token',BASE_TOKEN,'--table-id',TABLE_ID,'--record-id',item['record_id'],'--field-id',PRODUCT_IMAGE_FIELD,'--file-token',old,'--yes'],timeout=300)
    if code!=0: raise RuntimeError(raw)
    return old, raw

def chunks(seq,size):
    for i in range(0,len(seq),size): yield seq[i:i+size]

def main():
    targets=json.loads((RUN_ROOT/'logs'/'heic_targets.json').read_text(encoding='utf-8'))
    state=load_state()
    work=[t for t in targets if state.get(t['product_id'],{}).get('status')!='replaced']
    event('start',{'product_id':'','status':f'work={len(work)} batch_size={BATCH_SIZE}'})
    for bi,batch in enumerate(chunks(work,BATCH_SIZE),start=1):
        event('batch_start',{'product_id':'','status':f'batch={bi} count={len(batch)}'})
        state=load_state(); submitted=[]
        for item in batch:
            pn=item['product_id']
            try:
                task=submit_task(item)
                state[pn]={**state.get(pn,{}),'status':'submitted','task_id':task,'record_id':item['record_id'],'prepared_path':item['prepared_path'],'old_uploaded_file_token':item.get('old_uploaded_file_token','')}
                save_state(state); submitted.append({'item':item,'task_id':task})
                event('submitted',{'product_id':pn,'status':'submitted','task_id':task})
                time.sleep(1)
            except Exception as e:
                state[pn]={**state.get(pn,{}),'status':'failed_submit','error':str(e)[:1000]}; save_state(state)
                event('failed_submit',{'product_id':pn,'status':'failed_submit','error':str(e)[:500]})
        remaining={x['task_id']:x for x in submitted}; generated=[]; deadline=time.time()+BATCH_TIMEOUT
        while remaining and time.time()<deadline:
            for task,meta in list(remaining.items()):
                pn=meta['item']['product_id']
                try:
                    result=query_task(task); status=helper.extract_status(result)
                    if status=='completed':
                        (RUN_ROOT/'logs'/f'{safe_name(pn)}_wait.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                        url=result.get('data',{}).get('output',[{}])[0].get('url')
                        if not url: raise RuntimeError('completed without output url')
                        gen=download_generated(pn,url)
                        state[pn].update({'status':'generated','generated_path':str(gen),'generated_url':url}); save_state(state)
                        generated.append({'product_id':pn,'item':meta['item'],'task_id':task,'generated_path':str(gen)})
                        del remaining[task]
                        event('generated',{'product_id':pn,'status':'generated','task_id':task})
                    elif status=='failed':
                        state[pn].update({'status':'failed_generation','error':json.dumps(result,ensure_ascii=False)}); save_state(state)
                        del remaining[task]
                        event('failed_generation',{'product_id':pn,'status':'failed_generation','task_id':task})
                except BaseException as e:
                    event('query_warning',{'product_id':pn,'status':'query_warning','task_id':task,'error':str(e)[:300]})
            if remaining: time.sleep(POLL_INTERVAL)
        for task,meta in list(remaining.items()):
            pn=meta['item']['product_id']; state[pn].update({'status':'failed_timeout'}); save_state(state)
            event('failed_timeout',{'product_id':pn,'status':'failed_timeout','task_id':task})
        if generated:
            run_watermark(generated)
        for g in generated:
            item=g['item']; pn=g['product_id']; final=RUN_ROOT/'white-bg'/f'{safe_name(pn)}_white_bg_watermarked.png'
            try:
                qc=basic_qc(final)
                with QC_PATH.open('a',encoding='utf-8') as f: f.write(json.dumps({'product_id':pn,'file':str(final),**qc},ensure_ascii=False,separators=(',',':'))+'\n')
                if not qc['ok']: raise RuntimeError(f'basic qc failed: {qc}')
                new_token,upload_raw=upload_new(item,final)
                old_token,remove_raw=remove_old(item)
                row={'product_id':pn,'record_id':item['record_id'],'old_uploaded_file_token':old_token,'new_uploaded_file_token':new_token,'task_id':g['task_id'],'final_path':str(final),'status':'replaced','upload_raw':upload_raw,'remove_raw':remove_raw,'replaced_at':time.strftime('%Y-%m-%dT%H:%M:%S')}
                with UPLOAD_MANIFEST.open('a',encoding='utf-8') as f: f.write(json.dumps(row,ensure_ascii=False,separators=(',',':'))+'\n')
                append_registry({'product_id':pn,'record_id':item['record_id'],'settlement':'HEIC重跑替换','source_file_token':'','task_id':g['task_id'],'source_path':item['old_source_path'],'generated_path':g['generated_path'],'final_path':str(final),'upload_status':'uploaded','uploaded_file_token':new_token,'uploaded_at':row['replaced_at'],'run_root':str(RUN_ROOT)})
                state[pn].update({'status':'replaced','final_path':str(final),'new_uploaded_file_token':new_token,'old_removed_file_token':old_token,'replaced_at':row['replaced_at']}); save_state(state)
                event('replaced',{'product_id':pn,'status':'replaced','new_uploaded_file_token':new_token,'old_removed_file_token':old_token})
            except Exception as e:
                state[pn].update({'status':'failed_replace','error':str(e)[:1000],'final_path':str(final)}); save_state(state)
                event('failed_replace',{'product_id':pn,'status':'failed_replace','error':str(e)[:500]})
    state=load_state(); counts={}
    for v in state.values(): counts[v.get('status','unknown')]=counts.get(v.get('status','unknown'),0)+1
    event('finished',{'product_id':'','status':json.dumps(counts,ensure_ascii=False)})

if __name__=='__main__':
    main()
