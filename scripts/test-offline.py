from pathlib import Path
import os,sys,subprocess,json,re
P=Path(__file__).resolve().parents[1];W=P/'build/offline-tests';W.mkdir(parents=True,exist_ok=True)
bootstrap='''
import sys,os,socket,tempfile,unittest
from pathlib import Path
repo=Path(sys.argv[1]); name=sys.argv[2]
sys.path[:0]=[str(repo),str(repo/'tests')]
def forbidden(*a,**k):raise AssertionError('External connection forbidden in publication checks')
socket.socket.connect=forbidden
with tempfile.TemporaryDirectory(prefix='sanae-public-test-') as tmp:
 os.environ['SCE_BOT_TEST_MODE']='1'
 os.environ['LLBOT_GROUP']='1001';os.environ['SANAE_BOT_QQ']='1002'
 for key in ('SOCIAL_LITE_STATE','SLANG_PATH','FEEDBACK_PATH','SOCIAL_MEMORY_PATH','STICKER_MANIFEST','SANAE_SHARED_KNOWLEDGE_PATH','SOCIAL_LITE_ACTIVITY','PLAYER_BINDINGS_PATH','SOCIAL_ARCHIVE_ROOT','OPS_STATE_DIR','OPS_STATE_BASE'):
  os.environ[key]=str(Path(tmp)/key)
 os.chdir(tmp)
 result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromName(name))
 print('PUBLIC_RESULT',result.testsRun,len(result.failures),len(result.errors),len(result.skipped))
 os.chdir(repo)
 sys.exit(not result.wasSuccessful())
'''
results=[]
for f in sorted((P/'tests').glob('test_*.py')):
 r=subprocess.run([sys.executable,'-X','utf8','-c',bootstrap,str(P),f.stem],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',timeout=100)
 (W/(f.stem+'.log')).write_text(r.stdout,encoding='utf-8')
 m=re.search(r'PUBLIC_RESULT (\d+) (\d+) (\d+) (\d+)',r.stdout)
 row={'test':f.stem,'exitCode':r.returncode,'counts':list(map(int,m.groups())) if m else None};results.append(row);print(json.dumps(row),flush=True)
 (W/'sanae-test-results.json').write_text(json.dumps(results,indent=2)+'\n')
sys.exit(any(r['exitCode'] for r in results))
