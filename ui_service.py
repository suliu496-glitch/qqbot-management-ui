"""Management operations reuse the live bot's config, database and caches."""
import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import ui_runtime as runtime


def text(data, key, limit=500, required=False):
    value = data.get(key, '')
    if value is None and not required:
        value = ''
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f'{key} 格式或长度不正确')
    value = value.strip()
    if required and not value:
        raise ValueError(f'请填写 {key}')
    return value


def qq_id(value):
    value = str(value)
    if not re.fullmatch(r'[1-9][0-9]{4,14}', value):
        raise ValueError('请输入有效的 QQ 号或群号')
    return value


def revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def task_fields(data, now=None):
    now = now or datetime.now()
    kind = text(data, 'task_type', required=True)
    trigger = text(data, 'trigger_time')
    hours = 0
    if kind == 'daily':
        if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', trigger):
            raise ValueError('每日任务时间格式应为 HH:MM')
        h, m = map(int, trigger.split(':'))
        first = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if first <= now:
            first += timedelta(days=1)
    elif kind == 'once':
        try:
            first = datetime.fromisoformat(trigger)
        except ValueError:
            raise ValueError('请选择单次任务的日期和时间')
        if first.tzinfo is not None or first <= now:
            raise ValueError('单次任务必须使用本机时区的未来时间')
        trigger = first.isoformat()
    elif kind == 'interval':
        try:
            hours = int(data.get('interval_hours', 0))
        except (TypeError, ValueError):
            raise ValueError('间隔必须是整数小时')
        if not 1 <= hours <= 8760:
            raise ValueError('间隔应在 1 至 8760 小时之间')
        first = now + timedelta(hours=hours)
        trigger = ''
    else:
        raise ValueError('不支持的触发类型')
    target_type = text(data, 'target_type')
    if target_type not in ('private', 'group'):
        raise ValueError('接收对象应为私聊或群聊')
    action = text(data, 'action_type')
    if action not in ('ai_generate', 'static'):
        raise ValueError('此任务使用特殊动作，请通过原有工具管理')
    return dict(name=text(data, 'name', 100, True), task_type=kind, trigger_time=trigger,
                interval_hours=hours, target_type=target_type, target_id=qq_id(data.get('target_id', '')),
                content=text(data, 'content', 8000, True), action_type=action, first_trigger=first.isoformat())


class ManagementService:
    def __init__(self):
        import config
        import db
        import users
        self.c, self.db, self.users = config, db, users
        self.sent = {}

    def persona_text(self):
        # Edit the complete source, never the decorated/truncated prompt layer.
        if self.c.PERSONA_FILE.exists():
            return self.c.PERSONA_FILE.read_text('utf-8')
        return self.c._build_default_persona().split('\n', 1)[-1]

    def rows(self, sql, args=()):
        return [dict(r) for r in self.db._get_conn().execute(sql, args).fetchall()]

    def audit(self, action, target='', detail=''):
        self.db.log_audit('ui.' + action, 'local-console', detail, target)

    def profile(self, qq):
        profile = self.db.get_profile(qq) or {'qq': qq}
        profile = dict(profile)
        slots = self.db.get_nickname_slots(qq)
        profile.update(qq=qq, name=self.c._resolve_speaker_name(qq),
                       nickname=slots.get(slots.get('active_slot', '') + '_value', '') or slots.get('slot_a_value', ''),
                       relation=slots.get('relation', ''), tags=self.db.get_person_tags(qq),
                       facts=self.db.get_key_facts(qq), admin=qq in self.c._get_master_ids(),
                       muted=self.users.get_mute_status(qq))
        profile['legacy_notes'] = self.c._load_memory(f'private_{qq}').get('notes', [])
        profile['notes_revision'] = revision(profile['legacy_notes'])
        # A live countdown must not invalidate an otherwise unchanged draft.
        profile['revision'] = revision({k:v for k,v in profile.items() if k != 'muted'})
        return profile

    def contacts(self):
        ids = set(self.c._friend_cache)
        ids.update(r['qq'] for r in self.rows('SELECT qq FROM user_profile UNION SELECT qq FROM nickname_slots'))
        return [dict(qq=q, name=self.c._resolve_speaker_name(q), city=(self.db.get_profile(q) or {}).get('city', ''),
                     admin=q in self.c._get_master_ids()) for q in sorted(ids) if q.isdigit()]

    def admins(self):
        configured = set(self.c.MASTER_QQ_EXTRA)
        return [dict(qq=q, name=self.c._resolve_speaker_name(q), configured=q in configured,
                     source='配置指定' if q in configured else '动态名单 / 兜底') for q in sorted(self.c._get_master_ids())]

    def tasks(self):
        tasks = self.db.get_scheduled_tasks()
        for task in tasks:
            task['revision'] = revision(task)
            task['target_name'] = self.c._group_cache.get(task['target_id'], task['target_id']) if task['target_type'] == 'group' else self.c._resolve_speaker_name(task['target_id'])
        return tasks

    def read(self, name, query):
        arg = lambda k, default='': query.get(k, [default])[0]
        if name == 'overview':
            count = self.rows('SELECT COUNT(*) n FROM global_chat_log WHERE send_time >= ?', (datetime.now().date().isoformat(),))[0]['n']
            return dict(name=self.c.SELF_NICK, ws=runtime.ws_connected, paused=runtime.paused,
                        uptime=int(time.time()-runtime.started), ai_configured=bool(self.c.DEEPSEEK_API_KEY),
                        messages=count, contacts=len(self.contacts()), admins=len(self.admins()),
                        tasks=self.tasks(), recent=self.rows('SELECT * FROM global_chat_log ORDER BY id DESC LIMIT 6'),
                        test_mode=runtime.test_mode)
        if name == 'contacts':
            return self.contacts()
        if name == 'contact':
            return self.profile(qq_id(arg('qq')))
        if name == 'admins':
            return self.admins()
        if name == 'tasks':
            return self.tasks()
        if name == 'groups':
            return [dict(qq=q, name=n) for q, n in self.c._group_cache.items()]
        if name == 'chats':
            # Conversation JSON is scoped by target; global log has no outgoing recipient column.
            target = qq_id(arg('target'))
            kind = 'group' if arg('type') == 'group' else 'private'
            key = f'{kind}_{target}'
            mem = self.c._load_memory(key)
            conv = mem.get('conversation', [])
            if kind == 'private':
                conv = [m for m in conv if str(m.get('group_id', '0')) in ('0', '')]
            elif not conv:
                for file in self.c.DATA_DIR.glob(f'group_{target}_person_*.json'):
                    try:
                        conv = conv + json.loads(file.read_text('utf-8')).get('conversation', [])
                    except (ValueError, OSError):
                        pass
                conv = sorted(conv, key=lambda m: m.get('time', ''))
            needle = arg('q').casefold()
            conv = [m for m in conv if needle in str(m.get('content', '')).casefold()]
            return dict(messages=conv[-200:], muted=self.c._is_chat_muted(key), target=target, type=kind,
                        legacy_unscoped=kind == 'private' and any('group_id' not in m for m in conv))
        if name == 'records':
            qq = qq_id(arg('qq'))
            return dict(todos=self.db.get_todos(qq), schedules=self.db.get_schedules(qq), rules=self.db.get_auto_reply_rules(qq))
        if name == 'logs':
            from monitor import Monitor
            import sys
            bot = sys.modules.get('__main__')
            deep = []
            if hasattr(bot, '_deep_lock'):
                with bot._deep_lock:
                    deep = [dict(id=k, **v) for k, v in bot._deep_running.items()]
            return dict(audit=self.db.get_audit_log(100), metrics=Monitor.report(), deep=deep)
        if name == 'settings':
            return dict(persona=self.persona_text(), persona_revision=revision(self.persona_text()), model=self.c.DEEPSEEK_MODEL,
                        base=self.c.DEEPSEEK_BASE_URL, napcat=self.c.NAP_CAT_HTTP,
                        ai_key=bool(self.c.DEEPSEEK_API_KEY), vision_key=bool(self.c.ZHIPU_API_KEY),
                        timezone=time.tzname[0], launch=str(Path(__file__).parent / 'start-qq-bot.bat'),
                        stop=str(Path(__file__).parent / 'stop-qq-bot.bat'),
                        reset='备份后手动处理 data 目录')
        if name == 'tools':
            from tools import TOOLS, _MASTER_ONLY_TOOLS
            return [dict(name=t['function']['name'], description=t['function'].get('description', ''),
                         admin=t['function']['name'] in _MASTER_ONLY_TOOLS) for t in TOOLS]
        raise ValueError('接口不存在')

    def write(self, name, data):
        if name.startswith('task/'):
            if not runtime.task_lock.acquire(timeout=0.2):
                raise ValueError('调度器正在执行定时任务，请稍后再修改')
            try:
                return self._write(name, data)
            finally:
                runtime.task_lock.release()
        return self._write(name, data)

    def _write(self, name, data):
        # This is a local owner console. No caller-supplied QQ is accepted as authority.
        with runtime.lock:
            if name == 'pause':
                runtime.paused = bool(data.get('paused'))
                self.audit('pause', detail=str(runtime.paused))
            elif name == 'contact/save':
                qq = qq_id(data.get('qq', ''))
                current = self.profile(qq)
                if data.get('revision') != current['revision']:
                    raise ValueError('联系人已发生变化，请刷新后重新编辑')
                nickname = text(data, 'nickname', 30)
                fields = {k: text(data, k, 8000 if k in ('free_notes', 'user_summary', 'persona', 'facts') else 100)
                          for k in ('relation', 'city', 'fixed_name', 'real_name', 'free_notes', 'user_summary', 'persona', 'facts', 'tags')}
                if nickname and nickname != current.get('nickname'):
                    result = self.c._set_nickname(qq, nickname, 'local-console')
                    if not result['ok']:
                        raise ValueError(result['message'])
                elif not nickname and current.get('nickname'):
                    raise ValueError('称呼不能为空；请设置一个新称呼')
                conn = self.db._get_conn()
                conn.execute('INSERT OR IGNORE INTO user_profile (qq) VALUES (?)', (qq,))
                conn.execute('INSERT OR IGNORE INTO nickname_slots (qq) VALUES (?)', (qq,))
                with conn:
                    conn.execute('UPDATE nickname_slots SET relation=? WHERE qq=?', (fields['relation'], qq))
                    conn.execute('UPDATE user_profile SET city=?,fixed_name=?,real_name=?,free_notes=?,user_summary=?,persona=?,key_facts=?,last_update=? WHERE qq=?',
                                 (fields['city'], fields['fixed_name'], fields['real_name'], fields['free_notes'], fields['user_summary'], fields['persona'],
                                  json.dumps([f.strip() for f in fields['facts'].splitlines() if f.strip()], ensure_ascii=False), datetime.now().isoformat(), qq))
                    if fields['fixed_name'] != (current.get('fixed_name') or ''):
                        conn.execute('DELETE FROM temp_name_cache WHERE user_qq=?', (qq,))
                    conn.execute('DELETE FROM person_tags WHERE qq=?', (qq,))
                    for tag in set(re.split('[,，]', fields['tags'])):
                        if tag.strip():
                            conn.execute('INSERT OR IGNORE INTO person_tags (qq,tag) VALUES (?,?)', (qq, tag.strip()))
                self.c._invalidate_person_index()
                self.c._invalidate_profile_cache(qq)
                self.audit('contact.save', qq, '修改称呼/资料/记忆（无通知消息）')
            elif name in ('note/save', 'note/delete'):
                qq = qq_id(data.get('qq', ''))
                key = f'private_{qq}'
                with self.c._get_memory_lock(key):
                    mem = self.c._load_memory(key)
                    notes = mem.get('notes', [])
                    if data.get('revision') != revision(notes):
                        raise ValueError('备注已变化，请刷新后重试')
                    try:
                        index = int(data.get('index', -1))
                    except (ValueError, TypeError):
                        raise ValueError('备注编号不正确')
                    if index < -1 or index >= len(notes):
                        raise ValueError('备注已不存在')
                    prefixes = ('称呼:', '名字:', '关系:', '主人')
                    if index >= 0 and str(notes[index].get('note', '')).startswith(prefixes):
                        raise ValueError('这是结构化资料或权限历史，请在称呼、关系或管理员页面管理')
                    if name == 'note/delete':
                        if index < 0:
                            raise ValueError('请选择备注')
                        notes.pop(index)
                    else:
                        content = text(data, 'content', 4000, True)
                        valid, reason = self.c._validate_note(content)
                        if not valid:
                            raise ValueError(reason)
                        if content.startswith(prefixes):
                            raise ValueError('称呼、关系和权限请通过对应字段管理')
                        note = dict(time=datetime.now().isoformat(), note=content)
                        if index < 0:
                            notes.append(note)
                        else:
                            notes[index] = note
                    mem['notes'] = notes
                    self.c._save_memory(key, mem)
                self.c._invalidate_profile_cache(qq)
                self.audit(name, qq)
            elif name == 'contact/mute':
                qq = qq_id(data.get('qq', ''))
                try:
                    hours = float(data.get('hours', 0))
                except (TypeError, ValueError):
                    raise ValueError('静默时长不正确')
                if not 0 <= hours <= 8760:
                    raise ValueError('静默时长应在 0 至 8760 小时内')
                self.users.set_mute(qq, hours) if hours else self.users.cancel_mute(qq)
                self.audit('contact.mute', qq, str(hours))
            elif name in ('admin/add', 'admin/remove'):
                qq = qq_id(data.get('qq', ''))
                configured = set(self.c.MASTER_QQ_EXTRA)
                masters = set(self.c._get_master_ids())
                if name.endswith('remove'):
                    if qq in configured:
                        raise ValueError('此管理员由启动配置指定，请修改 MASTER_QQ 后重启')
                    if qq not in masters:
                        raise ValueError('此联系人已不是管理员')
                    if len(masters) <= 1:
                        raise ValueError('至少保留一位管理员')
                    masters.remove(qq)
                else:
                    masters.add(qq)
                # Central authority, independent of legacy role notes.
                file = self.c._MASTER_FILE
                tmp = file.with_suffix('.ui.tmp')
                tmp.write_text(json.dumps(sorted(masters - configured)), 'utf-8')
                tmp.replace(file)
                self.c._owner_cache = masters
                # Keep legacy role notes compatible with the existing QQ commands.
                # Authority still comes exclusively from the central admin set.
                key = f'private_{qq}'
                with self.c._get_memory_lock(key):
                    mem = self.c._load_memory(key)
                    notes = [n for n in mem.get('notes', []) if self.c.MASTER_MARK not in str(n.get('note', ''))]
                    if name.endswith('add'):
                        notes.append({'time':datetime.now().isoformat(), 'note':self.c.MASTER_MARK})
                    mem['notes'] = notes
                    self.c._save_memory(key, mem)
                self.c._invalidate_profile_cache(qq)
                self.audit(name, qq)
            elif name == 'task/save':
                fields = task_fields(data)
                tid = text(data, 'id')
                if tid:
                    old = self.db.get_scheduled_task(tid)
                    if old is None or data.get('revision') != revision(old):
                        raise ValueError('任务已变更或删除，请刷新后重试')
                    if old.get('action_type') not in ('static', 'ai_generate'):
                        raise ValueError('特殊任务请通过原有工具管理；可在此启停或删除')
                    # Preserve the next trigger when editing content only.
                    if all(old.get(k) == fields[k] for k in ('task_type', 'trigger_time', 'interval_hours')):
                        fields['first_trigger'] = old.get('first_trigger', fields['first_trigger'])
                    conn = self.db._get_conn()
                    with conn:
                        conn.execute('UPDATE scheduled_tasks SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=?', (*fields.values(), tid))
                else:
                    tid = self.db.add_scheduled_task(**{k:v for k,v in fields.items() if k != 'first_trigger'}, created_by='local-console')
                self.audit(name, tid)
            elif name in ('task/toggle', 'task/delete'):
                tid = text(data, 'id', required=True)
                old = self.db.get_scheduled_task(tid)
                if not old or data.get('revision') != revision(old):
                    raise ValueError('任务已变化，请刷新后重试')
                if name.endswith('delete'):
                    self.db.delete_scheduled_task(tid)
                else:
                    self.db.toggle_scheduled_task(tid, bool(data.get('enabled')))
                self.audit(name, tid)
            elif name == 'chat/mute':
                target = qq_id(data.get('target', ''))
                kind = 'group' if data.get('type') == 'group' else 'private'
                self.c._set_chat_muted(f'{kind}_{target}', bool(data.get('muted')))
                self.audit(name, f'{kind}_{target}')
            elif name == 'chat/send':
                target = qq_id(data.get('target', ''))
                kind = 'group' if data.get('type') == 'group' else 'private'
                content = text(data, 'content', 4000, True)
                request_id = text(data, 'request_id', 100, True)
                self.sent = {k:v for k,v in self.sent.items() if time.time()-v[0] < 3600}
                if request_id in self.sent:
                    return self.sent[request_id][1]
                if runtime.test_mode:
                    raise ValueError('隔离预览环境不允许发送消息')
                from messaging import _api
                self.sent[request_id] = (time.time(), {'ok': False, 'error': '此发送已提交；状态未知时请先核对QQ，勿重复发送'})
                # Structured text prevents accidental CQ-code execution.
                result = _api('send_group_msg' if kind == 'group' else 'send_private_msg',
                              {('group_id' if kind == 'group' else 'user_id'): int(target),
                               'message': [{'type':'text', 'data':{'text':content}}]})
                if result.get('status') != 'ok':
                    raise ValueError('QQ未确认发送成功，请先核对会话，避免重复发送')
                key = f'{kind}_{target}'
                with self.c._get_memory_lock(key):
                    mem = self.c._load_memory(key)
                    mem.setdefault('conversation', []).append(dict(time=datetime.now().strftime('%m-%d %H:%M'), direction='out', content=content))
                    self.c._save_memory(key, mem)
                self.audit(name, key, '人工回复已发送')
                self.sent[request_id] = (time.time(), {'ok': True})
            elif name == 'persona/save':
                content = text(data, 'persona', 30000, True)
                file = self.c.PERSONA_FILE
                if data.get('revision') != revision(self.persona_text()):
                    raise ValueError('人设已经变化，请刷新后重试')
                tmp = file.with_suffix('.ui.tmp')
                tmp.write_text(content, 'utf-8')
                if file.exists():
                    import shutil
                    shutil.copy2(file, file.with_suffix('.ui.bak'))
                tmp.replace(file)
                self.audit(name)
            elif name.startswith('record/'):
                self.write_record(name, data)
            else:
                raise ValueError('不支持的操作')
            return {'ok': True}

    def write_record(self, name, data):
        qq = qq_id(data.get('qq', ''))
        kind = text(data, 'kind')
        tables = {'todo':'todo', 'schedule':'schedule', 'rule':'auto_reply_rules'}
        if kind not in tables:
            raise ValueError('记录类型不正确')
        table = tables[kind]
        conn = self.db._get_conn()
        rid = text(data, 'id')
        if name == 'record/delete':
            with conn:
                conn.execute(f'DELETE FROM {table} WHERE id=? AND qq=?', (rid, qq))
        elif name == 'record/toggle' and kind in ('todo', 'rule'):
            field = 'done' if kind == 'todo' else 'enabled'
            with conn:
                conn.execute(f'UPDATE {table} SET {field}=? WHERE id=? AND qq=?', (int(bool(data.get('value'))), rid, qq))
        elif name == 'record/save':
            if kind == 'todo':
                fields = {'title': text(data, 'title', 500, True)}
            elif kind == 'schedule':
                when = text(data, 'when_time', 30, True)
                try:
                    datetime.fromisoformat(when)
                except ValueError:
                    raise ValueError('日程时间格式不正确')
                fields = {'title':text(data,'title',500,True),'when_time':when,'description':text(data,'description',2000)}
            else:
                fields = {'trigger':text(data,'trigger',300,True),'response':text(data,'response',4000,True)}
            with conn:
                if rid:
                    conn.execute(f'UPDATE {table} SET ' + ','.join(k+'=?' for k in fields) + ' WHERE id=? AND qq=?', (*fields.values(), rid, qq))
                else:
                    fields.update(id=uuid.uuid4().hex, qq=qq, created_at=datetime.now().isoformat())
                    conn.execute(f'INSERT INTO {table} (' + ','.join(fields) + ') VALUES (' + ','.join('?' for _ in fields) + ')', tuple(fields.values()))
        else:
            raise ValueError('不支持的记录操作')
        self.audit(name, qq, kind)
