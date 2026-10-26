"""No self-locking nested Store reads/writes during delete and retirement."""
import copy,json,sqlite3,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request,urlopen
from mixcut.server import Application,Store,Handler,ThreadingHTTPServer

class TransactionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.store=Store(self.root/'state')
 def test_nested_reads_writes_share_connection_and_commit_once(self):
  with self.store.connection() as outer:
   outer.execute('INSERT INTO records VALUES (?,?)',('first','1'))
   self.assertEqual(1,self.store.get('first'))
   self.store.put('second',2)
   with self.store.connection() as inner:self.assertIs(outer,inner)
   with sqlite3.connect(self.store.path,timeout=.1) as external:
    self.assertIsNone(external.execute('SELECT body FROM records WHERE id=?',('second',)).fetchone())
  self.assertEqual(2,self.store.get('second'))
 def test_outer_failure_rolls_back_nested_writes_and_next_call_recovers(self):
  with self.assertRaises(ValueError):
   with self.store.connection():
    self.store.put('temporary',123)
    raise ValueError('abort transaction')
  self.assertIsNone(self.store.get('temporary'));self.store.put('good',1);self.assertEqual(1,self.store.get('good'))
 def test_concurrent_threads_keep_independent_context_and_all_updates(self):
  self.store.put('batch:x',{'id':'x','count':0,'items':[]});errors=[]
  def worker():
   try:
    for _ in range(20):self.store.update('x',lambda b:b.update(count=b['count']+1))
   except Exception as e:errors.append(e)
  workers=[threading.Thread(target=worker) for _ in range(4)]
  for t in workers:t.start()
  for t in workers:t.join(timeout=5)
  self.assertTrue(all(not t.is_alive() for t in workers));self.assertEqual([],errors);self.assertEqual(80,self.store.get('batch:x')['count'])

class DeleteLockTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.app=Application(self.root/'state')
 def approved(self,identity):
  return {'id':identity,'index':1,'status':'success','music':[],'segments':[],
   'review':{'status':'approved','retained':True},'cleanup':{'output_retained':True,'temporary_segments_deleted':True}}
 def batch(self,bid,items):
  self.app.store.put('batch:'+bid,{'id':bid,'created_at':1,'status':'completed','config':{'output_dir':str(self.root/'outputs')},'items':items})
 def test_delete_mixed_batch_keeps_visible_survivor_and_hides_settled_approved(self):
  self.batch('a',[{'id':'delete','status':'pending','music':[],'segments':[]},self.approved('approved'),{'id':'keep','status':'failed','music':[],'segments':[]}])
  start=time.monotonic();result=self.app.forget_items([{'batch_id':'a','item_ids':['delete']}])
  self.assertLess(time.monotonic()-start,2);self.assertEqual(1,result['deleted_items']);self.assertEqual(['keep'],[i['id'] for i in result['updated_batches']['a']['items']])
 def test_prune_multiple_batches_after_first_write(self):
  for bid in ['a','b','c']:self.batch(bid,[self.approved('approved')])
  start=time.monotonic();self.app.prune_forgotten_items()
  self.assertLess(time.monotonic()-start,2);self.assertEqual([],self.app.store.batches())
 def test_real_http_delete_73_mixed_tasks_and_preserve_sources(self):
  raw=self.root/'raw.mp4';raw.write_bytes(b'raw');music=self.root/'song.mp3';music.write_bytes(b'music')
  approved=self.root/'approved';approved.mkdir();(approved/'keep.mp4').write_bytes(b'finished')
  items=[]
  for n in range(73):
   item=self.approved(str(n)) if n<25 else {'id':str(n),'status':('running' if n<28 else 'failed' if n<30 else 'pending'),'music':[],'segments':[{'path':str(raw)}]}
   items.append(item)
  self.batch('a',items);self.app.store.update('a',lambda b:b.update(status='running'))
  server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.app=self.app
  thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  try:
   req=Request(f'http://127.0.0.1:{server.server_port}/api/tasks/forget',data=json.dumps({'selections':[{'batch_id':'a','item_ids':[i['id'] for i in items]}]}).encode(),headers={'Content-Type':'application/json'})
   start=time.monotonic()
   with urlopen(req,timeout=5) as response:result=json.load(response)
   self.assertLess(time.monotonic()-start,2);self.assertEqual(73,result['deleted_items']);self.assertEqual([],self.app.visible_batches())
   self.assertTrue(raw.exists());self.assertTrue(music.exists());self.assertTrue((approved/'keep.mp4').exists())
  finally:server.shutdown();thread.join(timeout=2);server.server_close()
