"""Persist stage counters for the administrator task view."""
import time
from ..db import execute,dumps


class TaskProgress:
    def __init__(self,name,stages):
        self.name=name
        self.started=time.perf_counter()
        self.last_write=0
        self.stage=None
        self.phase={}
        self.stages={key:{'key':key,'label':label,'total':total,'completed':0,'failed':0,
                          'unit':unit,'model':model,'concurrency':concurrency,'duration':0.,
                          'started':None,'in_flight':{}} for key,label,total,unit,model,concurrency in stages}
        self.write(force=True)

    def begin(self,key,paper=None):
        value=self.stages[key]
        changed=self.stage!=key
        self.stage=key
        if value['started'] is None:value['started']=time.perf_counter()
        if paper:
            value['in_flight'][paper['id']]={'id':paper['id'],'title':paper.get('title','')[:160]}
        self.write(force=changed)

    def finish(self,key,*,completed=0,failed=0,seconds=0,paper=None):
        value=self.stages[key]
        value['completed']+=completed
        value['failed']+=failed
        value['duration']+=seconds
        if paper:value['in_flight'].pop(paper['id'],None)
        self.write()

    def set_phase(self,key,label,completed=0,total=0):
        changed=self.phase.get('phase')!=key
        self.phase={'phase':key,'phase_label':label,'phase_completed':completed,'phase_total':total} if key else {}
        self.write(force=changed)

    def value(self):
        stages=[]
        for value in self.stages.values():
            attempts=value['completed']+value['failed']
            elapsed=time.perf_counter()-value['started'] if value['started'] is not None else 0
            stages.append({k:v for k,v in value.items() if k not in ('duration','started','in_flight')} | {
                'pending':max(0,value['total']-attempts),
                'average_seconds':round(value['duration']/attempts,1) if attempts else None,
                'papers_per_minute':round(attempts/elapsed*60,1) if attempts and elapsed else None,
                'estimated_remaining_seconds':round(elapsed/attempts*(value['total']-attempts)) if attempts else None,
                'current_paper':next(reversed(value['in_flight'].values()),None),
                'in_flight':len(value['in_flight'])})
        current=next((v for v in stages if v['key']==self.stage),{})
        completed=sum(v['completed'] for v in stages)
        failed=sum(v['failed'] for v in stages)
        total=sum(v['total'] for v in stages)
        return {**current,'stages':stages,'stage':self.stage,'total':total,'completed':completed,'failed':failed,
                'pending':max(0,total-completed-failed),'unit':'篇' if len(stages)==1 else '项',
                'elapsed_seconds':round(time.perf_counter()-self.started,1),**self.phase}

    def write(self,force=False):
        if not force and time.perf_counter()-self.last_write<1:return
        self.last_write=time.perf_counter()
        execute('INSERT INTO source_status(name,progress) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET progress=excluded.progress',
                (self.name,dumps(self.value())))

    def close(self):
        for value in self.stages.values():value['in_flight'].clear()
        self.write(force=True)
