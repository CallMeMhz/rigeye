#!/usr/bin/env python
# -*- coding: utf-8 -*-
import time
import datetime
import operator
import atexit
from bson import ObjectId
from flask import Flask, request, redirect, url_for, render_template, jsonify
from pymongo import MongoClient
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

# 配置Flask
app = Flask(__name__)
app.config.update(dict(
    DEBUG=True,
    SECRET_KEY='development key',
    USERNAME='admin',
    PASSWORD='admin888'
))
app.config.from_envvar('RIGEYE_SETTINGS', silent=True)

# 连接数据库
DATABASE_URL = 'localhost'
DATABASE_PORT = 27017
client = MongoClient(host=DATABASE_URL, port=DATABASE_PORT)
db = client['rigeye']

# 初始化scheduler
scheduler = BackgroundScheduler()
scheduler.start()
atexit.register(lambda: scheduler.shutdown())


# 检查实例是否丢失信号
def check_instances_signal():

    for instance in db.instances.find():

        latest_data = db.data.find({'instance_id': str(instance['_id'])})
        latest_data = latest_data.sort([('time', -1)]).limit(1)
        latest_data = latest_data.next()

        now = time.time()

        if now - latest_data['time'] >= 5 and instance['status'] != 'NOSIGNAL':

            db.instances.update_one({'_id': instance['_id']}, {
                '$set': {
                    'status': 'NOSIGNAL'
                }
            }, True)

            db.events.insert({
                'level': 'danger',
                'title': 'Lose Instance Signal',
                'content': str(instance['_id']) + '.LOSE_SIGNAL',
                'createdAt': time.time()
            })

        elif (now - latest_data['time'] < 5 and instance['status'] != 'MONITORING'):

            db.instances.update_one({'_id': instance['_id']}, {
                '$set': {
                    'status': 'MONITORING'
                }
            }, True)

            db.events.insert({
                'level': 'success',
                'title': 'Recatch Instance Signal',
                'content': str(instance['_id']) + '.RECACHE_SIGNAL',
                'createdAt': time.time()
            })


# 添加定时任务
scheduler.add_job(
    func=check_instances_signal,
    trigger=IntervalTrigger(seconds=1),
    id='checking_job',
    name='Check signal from instances',
    replace_existing=True
)


# 将数据中的_id类型由ObjectId转化为String以Json化
def jsonifym(b):
    b['_id'] = str(b['_id'])
    return b


@app.template_filter('prettyTime')
def timectime(s):
    if isinstance(s, float):
        return format(datetime.datetime.fromtimestamp(s), '%Y-%m-%d %H:%M:%S')
    return s
    # return time.ctime(s)


# --- Router ---
@app.route('/')
def index():

    topn = []  # topN 实例流量排名

    for instance in db.instances.find():

        data = [data['net_speed_r'] for data in db.data.find({'instance_id': str(instance['_id'])})]

        topn.append({
            'name': str(instance['_id']),
            'data': sum(data)
        })

    topn.sort(key=operator.itemgetter('data'), reverse=True)  # 按照data值降序排序

    # 将data转化为数组作为图表数据
    for i in range(len(topn)):
        topn[i]['data'] = [topn[i]['data'], ]

    # 警告统计
    events_statistics = {
        'danger': db.events.find({'level': 'danger'}).count(),
        'success': db.events.find({'level': 'success'}).count()
    }

    return render_template(
        'index.html',
        topn=topn[:5],
        evts_stat=events_statistics
    )


@app.route('/instances')
def list_instances():
    instances = [jsonifym(instance) for instance in db.instances.find()]
    return render_template('instances.html', instances=instances)


@app.route('/dashboard')
def dashboard():
    panels = [jsonifym(panel) for panel in db.panels.find()]
    return render_template('dashboard.html', panels=panels)


@app.route('/dashboard/add')
def add_panel():
    instances = [instance for instance in db.instances.find()]
    return render_template('add_panel.html', instances=instances)


@app.route('/dashboard/insert', methods=['POST'])
def insert_panel():
    db.panels.insert({
        'instance_id': request.form['instance_id'],
        'title': request.form['title'],
        'module': request.form['module']
    })
    return redirect(url_for('dashboard'))


@app.route('/dashboard/remove/<panel_id>')
def remove_panel(panel_id):
    db.panels.remove(ObjectId(panel_id))
    return redirect(url_for('dashboard'))


@app.route('/events')
def list_events():
    events = [event for event in db.events.find().sort([('createdAt', -1)])]
    return render_template('events.html', events=events)


@app.route('/strategies')
def list_strategies():
    strategies = [strategy for strategy in db.strategies.find()]
    return render_template('strategies.html', strategies=strategies)


@app.route('/strategies/add')
def add_strategy():
    instances = [instance for instance in db.instances.find()]
    return render_template('add_strategy.html', instances=instances)


@app.route('/strategies/insert', methods=['POST'])
def insert_strategy():
    if not request.form['instance_id']:
        # flash 一个提示消息
        return redirect(url_for('add_strategy'))

    db.strategies.insert({
        'title': request.form['title'],
        'instance_id': request.form['instance_id'],
        # is_enalbe 单数为假 双数为真
        'is_enable': 0,
        'module': request.form['module'],
        'condition': request.form['condition'],
        'standard': int(request.form['standard']),
        'createdAt': time.time(),
        'createdBy': 'CallMeMhz'
    })

    return redirect(url_for('list_strategies'))


@app.route('/strategies/toggle', methods=['POST'])
def toggle_strategies_status():
    for strategy_id in request.form:
        db.strategies.update({'_id': ObjectId(strategy_id)}, {
            '$inc': {'is_enable': 1}
        })

    return redirect(url_for('list_strategies'))


@app.route('/strategies/remove', methods=['POST'])
def remove_strategies():
    for strategy_id in request.form:
        db.strategies.remove(ObjectId(strategy_id))
    return redirect(url_for('list_strategies'))


# --- RESTful API ---
def _compare_data(x, method, y):
    if method == 'gt':
        return x > y
    elif method == 'lt':
        return x < y


@app.route('/rest/add_data', methods=['POST'])
def add_data():
    data = request.json
    db.data.insert(data, w=1)

    # 检查数据是否触发警报
    for strategy in db.strategies.find({'instance_id': data['instance_id'], 'is_enable': {'$mod': [2, 0]}}):

        if _compare_data(data[strategy['module']], strategy['condition'], strategy['standard']):

            db.events.insert({
                'level': 'danger',
                'title': strategy['title'],
                'content': data['instance_id'] + '.' + strategy['module'] + '.' + str(strategy['standard']),
                'updatedAt': time.time(),
                'createdAt': time.time()
            })

            db.strategies.update({'_id': strategy['_id']}, {
                '$set': {
                    'latest_data': data[strategy['module']],
                    'latest_time': time.time()
                }
            }, True)

    return 'ok'


@app.route('/rest/add_info', methods=['POST'])
def add_info():
    info = request.json
    if 'instance_id' in info:
        instance = db.instances.find_one(ObjectId(info['instance_id']))
        if instance:
            return str(instance['_id'])
        else:
            info.pop('instance_id')

    instance = db.instances.insert(info, w=1)
    db.events.insert({
        'level': 'success',
        'title': 'Catch New Instance Signal',
        'content': str(instance) + '.CACHE_SIGNAL'
    })
    return str(instance)


@app.route('/rest/get_latest_data/<instance_id>', methods=['GET'])
def get_latest_data(instance_id):
    data = db.data.find({'instance_id': instance_id})
    data = data.sort([('time', -1)]).limit(1)
    if data.count():
        data = data.next()
        return jsonify(jsonifym(data))
    return 'err'


@app.route('/rest/get_60_data/<instance_id>/<module>', methods=['GET'])
def get_60s_data(instance_id, module):
    data = db.data.find({'instance_id': instance_id})
    data = data.sort([('time', -1)]).limit(60)
    res = [[item['time']*1000, item[module]] for item in data]
    return jsonify(res[::-1])


@app.route('/rest/get_info/<instance_id>', methods=['GET'])
def get_info(instance_id):
    instance = db.instances.find_one(ObjectId(instance_id))
    return jsonify(jsonifym(instance))


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80, threaded=True, debug=True)
