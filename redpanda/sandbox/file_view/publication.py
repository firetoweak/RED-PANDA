"""Explicit host transactions over the same block evidence used by Command tracking."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import uuid
import time
from . import native

class Conflict(RuntimeError):
    pass

def digest(data): return hashlib.sha256(data).hexdigest()
def load(path): return json.loads(Path(path).read_text(encoding='utf-8'))

def atomic(path, value):
    path = Path(path)
    temp = path.with_name(path.name+'.'+uuid.uuid4().hex+'.next')
    with temp.open('xb') as file:
        file.write(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf-8'))
        file.flush();os.fsync(file.fileno())
    # Windows readers can briefly deny DELETE sharing on internal metadata.
    # Retry the still-unpublished rename, never the Command or host writes.
    for delay in (0,0.01,0.02,0.04,0.08):
        if delay: time.sleep(delay)
        try:
            os.replace(temp,path)
        except OSError as error:
            if getattr(error,'winerror',None) not in (5,32) or delay==0.08: raise
        else:
            return

def fields(value, expected):
    if type(value) is not dict or set(value)!=set(expected): raise ValueError('persistent fields differ')

def command_id(value):
    if type(value) is not str or not 0<len(value)<=100 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in value): raise ValueError('invalid command id')

def valid_operation(value):
    if type(value) is not dict or 'kind' not in value: raise ValueError('invalid operation')
    if value['kind']=='execute': fields(value,('kind',))
    elif value['kind']=='restore':
        fields(value,('kind','targets','policy'))
        if type(value['targets']) is not list or not value['targets'] or value['policy'] not in ('original','preserve'): raise ValueError('invalid restore operation')
        for target_id in value['targets']: command_id(target_id)
        if len(set(value['targets']))!=len(value['targets']): raise ValueError('duplicate restore target')
    else: raise ValueError('unknown operation kind')

def valid_image(value):
    fields(value,('kind','size','identity','chunk_size','complete','blocks'))
    if value['kind'] not in ('file','directory','missing') or type(value['size']) is not int or value['size']<0 or type(value['chunk_size']) is not int or value['chunk_size']<=0 or type(value['complete']) is not bool or type(value['blocks']) is not dict: raise ValueError('invalid image')
    if value['identity'] is not None and type(value['identity']) is not str: raise ValueError('invalid identity')
    if value['kind']!='file' and (value['size'] or value['blocks'] or not value['complete']): raise ValueError('invalid non-file image')
    for key,hash_value in value['blocks'].items():
        if type(key) is not str or not key.isascii() or not key.isdigit() or str(int(key))!=key: raise ValueError('invalid block index')
        if int(key)*value['chunk_size']>=value['size']:
            if hash_value is not None: raise ValueError('block beyond EOF')
        elif type(hash_value) is not str or len(hash_value)!=64 or any(c not in '0123456789abcdef' for c in hash_value): raise ValueError('invalid block digest')
    if value['kind']=='file' and value['complete']:
        count=(value['size']+value['chunk_size']-1)//value['chunk_size']
        if any(str(i) not in value['blocks'] for i in range(count)): raise ValueError('incomplete full image')
    return value

def image(kind='missing',size=0,identity=None,chunk_size=65536,complete=True,blocks=None):
    return {'kind':kind,'size':size,'identity':identity,'chunk_size':chunk_size,'complete':complete,'blocks':{} if blocks is None else blocks}

def blob(store, value, key):
    offset=int(key)*value['chunk_size']
    if offset>=value['size']: return b''
    hash_value=value['blocks'].get(str(key))
    if hash_value is None: raise ValueError('required block evidence is absent')
    length=min(value['chunk_size'],value['size']-offset)
    with (store/'cas'/hash_value).open('rb') as file: data=file.read(length+1)
    if len(data)!=length or digest(data)!=hash_value: raise ValueError('CAS evidence is corrupt')
    return data

def save_block(store,data):
    value=digest(data);path=store/'cas'/value
    if not path.exists():
        with path.open('xb') as file:
            file.write(data);file.flush();os.fsync(file.fileno())
    else:
        with path.open('rb') as file: stored=file.read(len(data)+1)
        if len(stored)!=len(data) or digest(stored)!=value: raise ValueError('CAS evidence is corrupt')
    return value

def check_blobs(store,value):
    valid_image(value)
    for key in value['blocks']: blob(store,value,key)

def transaction_record(value):
    fields(value,('version','kind','commands','commit','steps','state','finalized'))
    if type(value['version']) is not int or value['version']!=3 or value['kind'] not in ('publish','undo') or value['state'] not in ('prepared','conflict','complete') or type(value['finalized']) is not bool: raise ValueError('invalid transaction')
    if type(value['commands']) is not list or not value['commands'] or type(value['steps']) is not list: raise ValueError('invalid transaction members')
    for command in value['commands']: command_id(command)
    if len(set(value['commands']))!=len(value['commands']) or (value['kind']=='undo' and len(value['commands'])!=1): raise ValueError('invalid transaction commands')
    if type(value['commit']) is not str or str(uuid.UUID(value['commit']))!=value['commit']: raise ValueError('invalid transaction commit')
    if value['finalized'] and value['state']!='complete': raise ValueError('invalid finalized transaction')
    for step in value['steps']:
        fields(step,('path','expected','desired','state','counts','created_identity'))
        if step['state'] not in ('prepared','applying','done') or type(step['path']) is not str: raise ValueError('invalid step')
        valid_image(step['expected']);valid_image(step['desired'])
        fields(step['counts'],('changed_bytes','preserved_bytes'))
        if any(type(v) is not int or v<0 for v in step['counts'].values()): raise ValueError('invalid counts')
        if step['created_identity'] is not None and type(step['created_identity']) is not str: raise ValueError('invalid created identity')
    return value

def target(base, relative):
    if type(relative) is not str or not relative.startswith('/'): raise ValueError('invalid view path')
    parts=relative[1:].split('/')
    if any(not p or p in ('.','..') or any(c in p for c in '\\:') for p in parts): raise ValueError('invalid view path')
    path=base
    for part in parts:
        path=path/part
        try: attrs=path.lstat().st_file_attributes
        except FileNotFoundError: continue
        if attrs&0x400: raise Conflict('reparse path')
    return path

def coverage(a,b):
    if a['chunk_size']!=b['chunk_size']: raise ValueError('block geometry differs')
    return image('file',a['size'],chunk_size=a['chunk_size'],complete=a['complete'],blocks={k:None for k in a['blocks'].keys()|b['blocks'].keys()})

def actual(base,relative,template=None,store=None):
    path=target(base,relative)
    chunk_size=65536 if template is None else template['chunk_size']
    try:
        if path.is_dir(): return image('directory',identity=native.directory_identity(path),chunk_size=chunk_size),None
        with native.open_file(path) as file:
            size=os.fstat(file.fileno()).st_size
            value=image('file',size,native.identity(file),chunk_size,False)
            if template is not None and size==template['size']:
                indexes=range((size+chunk_size-1)//chunk_size) if template['complete'] else sorted(map(int,template['blocks']))
                for index in indexes:
                    offset=index*chunk_size;key=str(index)
                    if offset>=size: value['blocks'][key]=None;continue
                    file.seek(offset);data=file.read(min(chunk_size,size-offset))
                    if len(data)!=min(chunk_size,size-offset): raise ValueError('short host read')
                    value['blocks'][key]=save_block(store,data)
                value['complete']=template['complete']
        return value,None
    except FileNotFoundError: return image(chunk_size=chunk_size),None
    except OSError as error:
        if error.winerror==32: raise Conflict('file is busy') from error
        raise

def same(a,b):
    return a['kind']==b['kind'] and a['size']==b['size'] and {k:v for k,v in a['blocks'].items() if v is not None}=={k:v for k,v in b['blocks'].items() if v is not None}

def choose(store,before,after,current,mode):
    for value in (before,after,current): valid_image(value)
    source,destination=(after,before) if mode in ('original','preserve') else (before,after)
    if source['kind']==destination['kind']==current['kind']=='file' and current['size']==source['size']:
        blocks={};restored=preserved=0
        for key in sorted(source['blocks'].keys()|destination['blocks'].keys(),key=int):
            a,b,data=blob(store,source,key),blob(store,destination,key),blob(store,current,key)
            if len(a)>len(b):
                changed_tail=sum(x!=y for x,y in zip(a[len(b):],data[len(b):]))
                if mode=='publish' and changed_tail: raise Conflict('user content overlaps removed range')
                if mode=='preserve' and changed_tail: return dict(current),{'changed_bytes':0,'preserved_bytes':current['size']}
            result=bytearray(b)
            for i in range(min(len(a),len(b))):
                if a[i]==b[i]: result[i]=data[i];continue
                if mode=='publish' and data[i]!=a[i]: raise Conflict('user content overlaps publication')
                if mode=='preserve' and data[i]!=a[i]: result[i]=data[i];preserved+=1
                else: restored+=1
            restored+=abs(len(a)-len(b))
            blocks[key]=save_block(store,bytes(result)) if result else None
        return image('file',destination['size'],chunk_size=destination['chunk_size'],complete=destination['complete'],blocks=blocks),{'changed_bytes':restored,'preserved_bytes':preserved}
    if same(source,current): return dict(destination),{'changed_bytes':destination['size'],'preserved_bytes':0}
    if mode=='preserve': return dict(current),{'changed_bytes':0,'preserved_bytes':current['size']}
    if mode=='original' and current['kind']==source['kind'] and current['size']==source['size']: return dict(destination),{'changed_bytes':destination['size'],'preserved_bytes':0}
    raise Conflict('structure or content differs')

def unfinished(store):
    folder=Path(store)/'host'
    return [] if not folder.exists() else [p for p in folder.glob('*.json') if not transaction_record(load(p))['finalized']]

def read_index(store):
    path=store/'host-index.json'
    value=load(path) if path.exists() else {'version':3,'active':[],'bindings':{}}
    fields(value,('version','active','bindings'))
    if type(value['version']) is not int or value['version']!=3 or type(value['active']) is not list or type(value['bindings']) is not dict: raise ValueError('invalid host index')
    for command in value['active']: command_id(command)
    if len(set(value['active']))!=len(value['active']) or any(type(k) is not str or type(v) is not str for k,v in value['bindings'].items()): raise ValueError('invalid host index members')
    return value

def pending_receipts(client):
    info=client.request('info');next_commit=info['commit'];commands=[];seen=set()
    while next_commit is not None:
        if next_commit in seen: raise ValueError('cyclic commits')
        seen.add(next_commit)
        commit=load(client.store/'commits'/(next_commit+'.json'))
        fields(commit,('previous','digest','command_id','kind'))
        if commit['kind']=='rebase': break
        if commit['kind']!='accept': raise ValueError('invalid commit kind')
        commands.append(commit['command_id']);next_commit=commit['previous']
    receipts=[]
    for command in reversed(commands):
        status=client.request('status',command_id=command)
        if status['status']!='accepted': raise ValueError('selected command is not accepted')
        receipt=status['receipt'];fields(receipt,('version','command_id','parent_commit','digest','changes','operation'))
        if receipt['version']!=3 or receipt['command_id']!=command: raise ValueError('invalid receipt')
        valid_operation(receipt['operation'])
        for change in receipt['changes']:
            fields(change,('path','before','after'))
            check_blobs(client.store,change['before']);check_blobs(client.store,change['after'])
        receipts.append(receipt)
    return info,receipts

def build_plan(client,changes,mode,expected_bindings):
    plan=[]
    for change in changes:
        path=change['path'];before,after=change['before'],change['after']
        source,destination=(before,after) if mode=='publish' else (after,before)
        current,_=actual(client.base,path,coverage(source,destination),client.store)
        expected=expected_bindings.get(path)
        if current['kind'] in ('file','directory') and expected is not None and current['identity']!=expected: raise Conflict('file identity changed')
        if mode=='publish' and before['kind']=='file':
            identity=before['identity']
            if identity is not None and not identity.startswith('vfs:') and current['kind']=='file' and current['identity']!=identity: raise Conflict('file identity changed')
        desired,counts=choose(client.store,before,after,current,mode)
        check_blobs(client.store,desired)
        if current['kind']=='directory' and desired['kind']=='missing' and any(target(client.base,path).iterdir()):
            children={c['path'] for c in changes if c['after' if mode=='publish' else 'before']['kind']=='missing'}
            if any('/'+str(p.relative_to(client.base)).replace('\\','/') not in children for p in target(client.base,path).iterdir()):
                if mode=='preserve': desired=dict(current)
                else: raise Conflict('directory contains user children')
        plan.append({'path':path,'expected':current,'desired':desired,'state':'prepared','counts':counts,'created_identity':None})
    def order(step):
        depth=step['path'].count('/')
        if step['desired']['kind']=='directory' and step['expected']['kind']=='missing': return 0,depth,step['path']
        if step['desired']['kind']!='missing': return 1,depth,step['path']
        return 2,-depth,step['path']
    return sorted(plan,key=order)

def mixed(store,current,expected,desired):
    if any(v['kind']!='file' for v in (current,expected,desired)) or current['size'] not in (expected['size'],desired['size']): return False
    for key in current['blocks']:
        data,a,b=blob(store,current,key),blob(store,expected,key),blob(store,desired,key)
        if not all((i<len(a) and byte==a[i]) or (i<len(b) and byte==b[i]) for i,byte in enumerate(data)): return False
    return True

def matches_open(file,store,value):
    if os.fstat(file.fileno()).st_size!=value['size'] or native.identity(file)!=value['identity']: return False
    for key in value['blocks']:
        expected=blob(store,value,key)
        if not expected: continue
        file.seek(int(key)*value['chunk_size'])
        if file.read(len(expected))!=expected: return False
    return True

def apply_step(client,transaction,step,path):
    expected,desired=step['expected'],step['desired']
    template=coverage(expected,desired)
    current,_=actual(client.base,step['path'],template,client.store)
    if current['kind']=='file' and current['size']==desired['size'] and current['size']!=expected['size']:
        template['size']=desired['size'];current,_=actual(client.base,step['path'],template,client.store)
    owned_identity=step['created_identity'] or expected['identity']
    if current['kind'] in ('file','directory') and owned_identity is not None and current['identity']!=owned_identity: raise Conflict('file identity changed during transaction')
    if same(current,desired): step['state']='done';atomic(path,transaction);return
    if not same(current,expected):
        if step['state']!='applying' or not mixed(client.store,current,expected,desired): raise Conflict('transaction state changed externally')
    step['state']='applying';atomic(path,transaction)
    target_path=target(client.base,step['path'])
    if desired['kind']=='directory':
        target_path.mkdir()
        step['created_identity']=native.directory_identity(target_path);atomic(path,transaction)
    elif desired['kind']=='missing':
        if current['kind']=='directory':
            try: target_path.rmdir()
            except OSError as error:
                if error.winerror==145: raise Conflict('directory contains user children') from error
                raise
        elif current['kind']=='file':
            with native.open_file(target_path,write=True) as file:
                if not matches_open(file,client.store,current): raise Conflict('file changed before deletion')
                native.remove_open(file)
    else:
        create=current['kind']=='missing'
        with native.open_file(target_path,create=create,write=True) as file:
            if create:
                step['created_identity']=native.identity(file);atomic(path,transaction)
            elif not matches_open(file,client.store,current): raise Conflict('file changed before write')
            for key in sorted(desired['blocks'],key=int):
                data=blob(client.store,desired,key)
                if not data or (not create and desired['blocks'][key]==current['blocks'].get(key)): continue
                file.seek(int(key)*desired['chunk_size']);file.write(data)
            if create or current['size']!=desired['size']: file.truncate(desired['size'])
            file.flush();os.fsync(file.fileno())
    step['state']='done';atomic(path,transaction)

def execute(client,path):
    transaction=transaction_record(load(path))
    for step in transaction['steps']:
        target(client.base,step['path']);check_blobs(client.store,step['expected']);check_blobs(client.store,step['desired'])
    if transaction['finalized']: return transaction
    client.request('pause')
    try:
        for step in transaction['steps']:
            if step['state']=='done':
                current,_=actual(client.base,step['path'],step['desired'],client.store)
                identity=step['created_identity'] or step['expected']['identity']
                if not same(current,step['desired']) or (current['kind'] in ('file','directory') and identity is not None and current['identity']!=identity): raise Conflict('completed step changed externally')
            else: apply_step(client,transaction,step,path)
    except Conflict as error:
        transaction['state']='conflict';atomic(path,transaction)
        return {'status':'conflict','reason':str(error),'transaction':str(path),'completed':[s['path'] for s in transaction['steps'] if s['state']=='done']}
    transaction['state']='complete';atomic(path,transaction)
    index=read_index(client.store)
    if transaction['kind']=='publish':
        for command in transaction['commands']:
            if command not in index['active']: index['active'].append(command)
    else:
        command=transaction['commands'][0]
        if index['active'] and index['active'][-1]==command: index['active'].pop()
        elif command in index['active']: raise ValueError('undo ordering changed')
    for step in transaction['steps']:
        value,_=actual(client.base,step['path'])
        if value['kind'] in ('file','directory'): index['bindings'][step['path']]=value['identity']
        else: index['bindings'].pop(step['path'],None)
    atomic(client.store/'host-index.json',index)
    info=client.request('info')
    if info['commit']==transaction['commit']:
        result=client.request('rebase',expected_commit=transaction['commit'])
        if result['status']!='rebased': raise Conflict('view changed during host transaction')
    else:
        commit=load(client.store/'commits'/(info['commit']+'.json'))
        if commit['kind']!='rebase' or commit['previous']!=transaction['commit']: raise Conflict('view changed before rebase acknowledgment')
        client.request('rebase',expected_commit=info['commit'])
    transaction['finalized']=True;atomic(path,transaction)
    return transaction

def prepare(client,changes,commands,commit,mode):
    if unfinished(client.store): raise Conflict('unfinished transaction requires reconciliation')
    client.request('pause');index=read_index(client.store)
    try: steps=build_plan(client,changes,mode,index['bindings'] if mode!='publish' else {})
    except Conflict as error: return {'status':'conflict','reason':str(error),'completed':[]}
    folder=client.store/'host';folder.mkdir(exist_ok=True);path=folder/(uuid.uuid4().hex+'.json')
    transaction={'version':3,'kind':'publish' if mode=='publish' else 'undo','commands':commands,'commit':commit,'steps':steps,'state':'prepared','finalized':False}
    atomic(path,transaction);return execute(client,path)

def combine(first,next_change):
    before=dict(first['before']);before['blocks']=dict(before['blocks'])
    after=dict(next_change['after']);after['blocks']=dict(after['blocks'])
    if before['kind']==next_change['before']['kind']=='file':
        for key,value in next_change['before']['blocks'].items():
            if key not in before['blocks']: before['blocks'][key]=value if int(key)*before['chunk_size']<before['size'] else None
        before['complete']=before['complete'] or next_change['before']['complete']
    if after['kind']==first['after']['kind']=='file' and not after['complete']:
        previous=dict(first['after']['blocks']);previous.update(after['blocks'])
        after['blocks']={k:v if int(k)*after['chunk_size']<after['size'] else None for k,v in previous.items()}
        after['complete']=first['after']['complete']
    return {'path':first['path'],'before':before,'after':after}

def publish(client):
    info,receipts=pending_receipts(client)
    if not receipts: return {'status':'no_pending_commands'}
    combined={}
    for receipt in receipts:
        for change in receipt['changes']:
            path=change['path'];combined[path]=combine(combined[path],change) if path in combined else change
    return prepare(client,list(combined.values()),[r['command_id'] for r in receipts],info['commit'],'publish')

def undo(client,command_id,policy):
    if policy not in ('original','preserve'): raise ValueError('explicit undo policy required')
    info,pending=pending_receipts(client)
    if pending: raise Conflict('publish or discard pending view before undo')
    index=read_index(client.store)
    if not index['active'] or index['active'][-1]!=command_id: return {'status':'later_command_active' if command_id in index['active'] else 'not_active','written_bytes':0}
    receipt=client.request('status',command_id=command_id)['receipt']
    return prepare(client,receipt['changes'],[command_id],info['commit'],policy)
