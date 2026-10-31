import http.client
import json
import threading
import unittest
from datetime import datetime
from unittest.mock import patch

from ui_sandbox import make_service
from ui_service import task_fields, revision
from ui_server import create_server


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.sandbox, self.service = make_service()

    def tearDown(self):
        self.service.db.close_db()
        self.sandbox.cleanup()

    def test_all_read_pages(self):
        for name in ('overview','contacts','admins','tasks','groups','logs','settings','tools'):
            self.assertIsNotNone(self.service.read(name, {}), name)

    def test_contact_edit_persists_without_notification(self):
        p = self.service.profile('10000004')
        data = dict(p, nickname='林小同学', relation='同学', city='焦作', tags='英语,读书', facts='喜欢晨读', free_notes='新备注')
        with patch('messaging._api', side_effect=AssertionError('must not send')):
            self.service.write('contact/save', data)
        saved = self.service.profile('10000004')
        self.assertEqual(saved['nickname'], '林小同学')
        self.assertEqual(saved['free_notes'], '新备注')
        self.assertEqual(saved['facts'], ['喜欢晨读'])
        self.assertEqual(saved['relation'], '同学')
        with self.assertRaises(ValueError):
            self.service.write('contact/save', data)

    def test_duplicate_nickname_rejected(self):
        p = self.service.profile('10000005')
        with self.assertRaises(ValueError):
            self.service.write('contact/save', dict(p, nickname='小林', tags='', facts=''))

    def test_admin_authority_is_not_a_note(self):
        self.service.write('admin/add', {'qq':'10000004'})
        self.assertIn('10000004',self.service.c._get_master_ids())
        self.assertTrue(self.service.c._owner_remove('10000004'))
        self.assertTrue(self.service.c._owner_add('10000004'))
        self.service.write('admin/remove', {'qq':'10000004'})
        self.assertNotIn('10000004',self.service.c._get_master_ids())
        self.assertTrue(self.service.c._owner_add('10000004'))
        self.assertTrue(self.service.c._owner_remove('10000004'))
        with self.assertRaises(ValueError):
            self.service.write('admin/remove', {'qq':'10000001'})

    def test_tasks_crud_and_stale_guard(self):
        data=dict(name='测试',task_type='daily',trigger_time='07:30',target_type='private',target_id='10000004',content='提醒',action_type='ai_generate')
        self.service.write('task/save',data)
        task=next(t for t in self.service.tasks() if t['name']=='测试')
        first=task['first_trigger']
        self.service.write('task/save',dict(task,content='新内容'))
        updated=self.service.db.get_scheduled_task(task['id'])
        self.assertEqual(updated['first_trigger'],first)
        self.assertEqual(updated['content'],'新内容')
        with self.assertRaises(ValueError):
            self.service.write('task/delete',task)
        self.service.write('task/toggle',dict(updated,revision=revision(updated),enabled=False))
        paused=self.service.db.get_scheduled_task(task['id'])
        self.assertFalse(paused['enabled'])
        self.service.write('task/delete',dict(paused,revision=revision(paused)))
        self.assertIsNone(self.service.db.get_scheduled_task(task['id']))

    def test_task_time_validation(self):
        data=dict(name='提醒',task_type='daily',trigger_time='07:30',target_type='private',target_id='10000004',content='提醒',action_type='static')
        now=datetime(2026,9,20,8)
        self.assertEqual(task_fields(data,now)['first_trigger'],'2026-09-21T07:30:00')
        for override in ({'trigger_time':'25:99'},{'task_type':'once','trigger_time':'2020-01-01T00:00'}, {'task_type':'interval','interval_hours':0}, {'target_id':'../file'},{'action_type':'execute'}):
            with self.assertRaises(ValueError):
                task_fields(dict(data,**override),now)

    def test_persona_preserves_full_source(self):
        settings=self.service.read('settings',{})
        self.assertIn('## 人物规则',settings['persona'])
        self.assertNotIn('LAYER',settings['persona'])
        self.service.write('persona/save',dict(persona=settings['persona']+'\n追加规则',revision=settings['persona_revision']))
        self.assertIn('保留完整原文',self.service.c.PERSONA_FILE.read_text('utf-8'))

    def test_preview_cannot_send(self):
        with self.assertRaises(ValueError):
            self.service.write('chat/send',dict(target='10000004',type='private',content='test',request_id='test'))

    def test_scheduler_blocks_task_edits_but_not_other_management(self):
        import ui_runtime
        with ui_runtime.task_lock:
            with self.assertRaisesRegex(ValueError, '调度器'):
                self.service.write('task/delete', {'id':'unknown'})
            self.service.write('pause', {'paused':False})

    def test_records_scoped_to_person(self):
        self.service.write('record/save',dict(qq='10000004',kind='todo',title='新待办'))
        todo=self.service.db.get_todos('10000004')[-1]
        self.service.write('record/delete',dict(qq='10000005',kind='todo',id=todo['id']))
        self.assertTrue(any(t['id']==todo['id'] for t in self.service.db.get_todos('10000004')))
        self.service.write('record/toggle',dict(qq='10000004',kind='todo',id=todo['id'],value=True))
        self.assertTrue(self.service.db.get_todos('10000004')[-1]['done'])

    def test_legacy_notes_edit_keeps_conversation(self):
        p=self.service.profile('10000004')
        self.service.write('note/save',dict(qq=p['qq'],index=-1,revision=p['notes_revision'],content='喜欢早起'))
        p=self.service.profile(p['qq'])
        self.assertEqual(p['legacy_notes'][0]['note'],'喜欢早起')
        self.service.write('note/save',dict(qq=p['qq'],index=0,revision=p['notes_revision'],content='喜欢早起读书'))
        p=self.service.profile(p['qq'])
        self.assertEqual(len(self.service.c._load_memory('private_10000004')['conversation']),2)
        self.service.write('note/delete',dict(qq=p['qq'],index=0,revision=p['notes_revision']))
        self.assertEqual(self.service.profile(p['qq'])['legacy_notes'],[])

    def test_new_group_context_not_shown_as_private_chat(self):
        self.service.c._add_to_memory('private_10000004','10000004','小林','群消息','in',group_id='20000001')
        result=self.service.read('chats',{'target':['10000004'],'type':['private']})
        self.assertFalse(any(m['content']=='群消息' for m in result['messages']))
        self.assertTrue(result['legacy_unscoped'])

    def test_paused_bot_records_incoming_without_model_call(self):
        import bot
        import ui_runtime
        ui_runtime.paused=True
        event=dict(message_type='private',target_id='10000004',sender_id='10000004',sender_nick='小林',content='暂停时的消息')
        try:
            with patch('bot._call_deepseek',side_effect=AssertionError('no model call')), patch('bot._send_text',side_effect=AssertionError('no message')):
                bot._do_think_and_reply(event)
            self.assertEqual(self.service.c._load_memory('private_10000004')['conversation'][-1]['content'],'暂停时的消息')
        finally:
            ui_runtime.paused=False


class HTTPTests(unittest.TestCase):
    def setUp(self):
        class Stub:
            def read(self,*args): return {'ok':True}
            def write(self,*args): return {'ok':True}
        self.server=create_server(Stub(),0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.port=self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self,method,path,data=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.port)
        body=json.dumps(data) if data is not None else None
        h={'Content-Type':'application/json',**(headers or {})}
        conn.request(method,path,body,h)
        response=conn.getresponse()
        result=(response.status,response.read(),response.getheader('Set-Cookie'))
        conn.close()
        return result

    def test_auth_origin_and_csrf(self):
        self.assertEqual(self.request('GET','/api/contacts')[0],401)
        self.assertEqual(self.request('GET','/api/contacts',headers={'Host':'evil.example'})[0],403)
        token=self.server.bootstrap_url.split('token=')[1]
        status,body,cookie=self.request('POST','/api/login',{'token':token})
        self.assertEqual(status,200)
        csrf=json.loads(body)['csrf']
        headers={'Cookie':cookie.split(';')[0]}
        self.assertEqual(self.request('POST','/api/pause',{'paused':True},headers)[0],403)
        headers['X-CSRF-Token']=csrf
        self.assertEqual(self.request('POST','/api/pause',{'paused':True},headers)[0],200)
        headers['Origin']='https://evil.example'
        self.assertEqual(self.request('POST','/api/pause',{'paused':True},headers)[0],403)

    def test_no_filesystem_escape(self):
        for path in ('/.env','/../config.py','/data/bot.db'):
            self.assertEqual(self.request('GET',path)[0],404)
        self.assertEqual(self.request('GET','/')[0],200)


if __name__=='__main__':
    unittest.main()
