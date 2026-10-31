"""Isolated fixtures. Never open or migrate the user's production database."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def make_service():
    sandbox = tempfile.TemporaryDirectory(prefix='qqbot-ui-test-')
    folder = Path(sandbox.name)
    os.environ.update(DATA_DIR=str(folder), PERSONA_FILE=str(folder/'persona.txt'),
                      MASTER_QQ='10000001', MY_QQ='10000000', DEEPSEEK_API_KEY='', ZHIPU_API_KEY='')
    import config as c
    import db
    import ui_runtime
    from ui_service import ManagementService
    db.close_db()
    db.DB_PATH = folder/'bot.db'
    c.DATA_DIR = folder
    c.PERSONA_FILE = folder/'persona.txt'
    c._MASTER_FILE = folder/'masters.json'
    c.MASTER_QQ_EXTRA = ['10000001']
    c._owner_cache = None
    c._profile_cache.clear()
    c._friend_cache.clear()
    c._friend_cache.update({'10000001':'管理员甲','10000004':'林同学','10000005':'陈同学'})
    c._group_cache.clear()
    c._group_cache['20000001'] = '英语学习小组'
    ui_runtime.test_mode = True
    db.init_db()
    for qq, name in c._friend_cache.items():
        db.set_fixed_name(qq, name)
    db.set_free_notes('10000004','正在准备英语考试，喜欢早晨学习。')
    db.set_person_city('10000004','焦作')
    db.set_person_tags('10000004',['英语学习','读书'])
    c._set_nickname('10000004','小林','10000001')
    c.PERSONA_FILE.write_text('自然、简短地回复。\n\n## 人物规则\n保留完整原文。', 'utf-8')
    c._save_memory('private_10000004',{'conversation':[{'time':'09-20 10:24','direction':'in','content':'明天早上提醒我背单词。'},{'time':'09-20 10:25','direction':'out','content':'好呀，想几点提醒？'}]})
    db.insert_chat_log('10000004','10000000','0','明天早上提醒我背单词。')
    db.add_scheduled_task('背单词提醒','daily','07:30',0,'private','10000004','提醒背单词','10000001')
    db.add_scheduled_task('阅读提醒','daily','21:30',0,'private','10000005','提醒阅读','10000001')
    db.add_todo('10000004','复习今天的单词')
    db.add_auto_reply_rule('10000004','早上好','早上好，今天也加油！')
    return sandbox, ManagementService()


if __name__ == '__main__':
    import json
    from ui_server import create_server
    sandbox, service = make_service()
    server = create_server(service, 8876)
    output = ROOT/'ui-test-output'
    output.mkdir(exist_ok=True)
    (output/'preview.json').write_text(json.dumps({'url':server.bootstrap_url}), 'utf-8')
    print('Isolated UI preview on port 8876', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
