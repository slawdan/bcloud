# Copyright (C) 2014 LiuLang <gsushzhsosgsu@gmail.com>
# Use of this source code is governed by GPLv3 license that can be found
# in http://www.gnu.org/licenses/gpl-3.0.html

import json
import multiprocessing
import os
from queue import Queue
import re
import threading
import time

from urllib import request
from gi.repository import GLib
from gi.repository import GObject

from bcloud.const import State
from bcloud.net import ForbiddenHandler
from bcloud import pcs

CHUNK_SIZE = 131072 # 128K
CHUNK_SIZE2 = 32768 # 32K
RETRIES = 5
THRESHOLD_TO_FLUSH = 100  # 磁盘写入数据次数超过这个值时, 就进行一次同步.

(NAME_COL, PATH_COL, FSID_COL, SIZE_COL, CURRSIZE_COL, LINK_COL,
    ISDIR_COL, SAVENAME_COL, SAVEDIR_COL, STATE_COL, STATENAME_COL,
    HUMANSIZE_COL, PERCENT_COL) = list(range(13))

BATCH_FINISISHED, BATCH_ERROR = -1, -2

def get_tmp_filepath(dir_name, save_name):
    '''返回最终路径名及临时路径名'''
    filepath = os.path.join(dir_name, save_name)
    return filepath, filepath + '.part', filepath + '.bcloud-stat'


class DownloadBatch(threading.Thread):

    def __init__(self, id_, queue, url, lock, start_size, end_size, fh, timeout):
        super().__init__()
        self.id_ = id_
        self.queue = queue
        self.url = url
        self.lock = lock
        self.start_size = start_size
        self.end_size = end_size
        self.fh = fh
        self.timeout = timeout
        self.stop_flag = False

    def run(self):
        self.download()

    def stop(self):
        self.stop_flag = True

    def download(self):
        opener = request.build_opener()
        content_range = 'bytes={0}-{1}'.format(self.start_size, self.end_size)
        opener.addheaders = [('Range', content_range)]
        try:
            req = opener.open(self.url, timeout=self.timeout)
        except OSError:
            self.queue.put((self.id_, BATCH_ERROR))
            return

        offset = self.start_size
        while not self.stop_flag:
            try:
                block = req.read(CHUNK_SIZE2)
            except OSError:
                self.queue.put((self.id_, BATCH_ERROR))
                return
            if not block:
                with self.lock:
                    self.queue.put((self.id_, BATCH_FINISISHED))
                break
            with self.lock:
                if self.fh.closed:
                    break
                self.fh.seek(offset)
                self.fh.write(block)
                self.queue.put((self.id_, len(block)), block=False)
                #self.queue.put((self.id_, len(block)))
                offset = offset + len(block)


class Downloader(threading.Thread, GObject.GObject):
    '''管理每个下载任务, 使用了多线程下载.

    当程序退出时, 下载线程会保留现场, 以后可以继续下载.
    断点续传功能基于HTTP/1.1 的Range, 百度网盘对它有很好的支持.
    '''

    __gsignals__ = {
            'started': (GObject.SIGNAL_RUN_LAST,
                # fs_id
                GObject.TYPE_NONE, (str, )),
            'received': (GObject.SIGNAL_RUN_LAST,
                # fs-id, current-size
                GObject.TYPE_NONE, (str, GObject.TYPE_INT64)),
            'downloaded': (GObject.SIGNAL_RUN_LAST, 
                # fs_id
                GObject.TYPE_NONE, (str, )),
            'disk-error': (GObject.SIGNAL_RUN_LAST,
                # fs_id, tmp_filepath
                GObject.TYPE_NONE, (str, str)),
            'network-error': (GObject.SIGNAL_RUN_LAST,
                # fs_id
                GObject.TYPE_NONE, (str, )),
            }

    def __init__(self, parent, row):
        threading.Thread.__init__(self)
        self.daemon = True
        GObject.GObject.__init__(self)

        self.cookie = parent.app.cookie
        self.tokens = parent.app.tokens
        self.default_threads = int(parent.app.profile['download-segments'])
        self.timeout = int(parent.app.profile['download-timeout'])
        self.row = row[:]

    def download(self):
        row = self.row
        if not os.path.exists(row[SAVEDIR_COL]):
            os.makedirs(row[SAVEDIR_COL], exist_ok=True)
        filepath, tmp_filepath, conf_filepath = get_tmp_filepath(
                row[SAVEDIR_COL], row[SAVENAME_COL]) 

        if os.path.exists(filepath):
            print('file exists:', filepath)
            self.emit('downloaded', row[FSID_COL])
            # TODO: ask to confirm overwriting
            # File exists, do nothing
            return

        url = pcs.get_download_link(self.cookie, self.tokens, row[PATH_COL])
        if not url:
            print('Error: Failed to get download link')
            row[STATE_COL] = State.ERROR
            self.emit('network-error', row[FSID_COL])
            return

        if os.path.exists(conf_filepath) and os.path.exists(tmp_filepath):
            with open(conf_filepath) as conf_fh:
                status = json.load(conf_fh)
            threads = len(status)
            file_exists = True
            fh = open(tmp_filepath, 'ab')
        else:
            req = request.urlopen(url)
            if not req:
                self.emit('network-error', row[FSID_COL])
                return
            content_length = req.getheader('Content-Length')
            # Fixed: baiduPCS using non iso-8859-1 codec in http headers
            if not content_length:
                match = re.search('\sContent-Length:\s*(\d+)', str(req.headers))
                if not match:
                    self.emit('network-error', row[FSID_COL])
                    return
                content_length = match.group(1)
            size = int(content_length)
            threads = self.default_threads
            average_size, pad_size = divmod(size, threads)
            file_exists = False
            status = []
            fh = open(tmp_filepath, 'wb')
            fh.truncate(size)

        # task list
        tasks = []
        # message queue
        queue = Queue()
        # threads lock
        lock = threading.RLock()
        for id_ in range(threads):
            if file_exists:
                start_size, end_size, received = status[id_]
                start_size += received
            else:
                start_size = id_ * average_size
                end_size = start_size + average_size - 1
                if id_ == threads - 1:
                    end_size = end_size + pad_size + 1
                status.append([start_size, end_size, 0])
            task = DownloadBatch(id_, queue, url, lock, start_size, end_size,
                                 fh, self.timeout)
            tasks.append(task)

        for task in tasks:
            task.start()

        try:
            conf_count = 0
            done = 0
            self.emit('started', row[FSID_COL])
            while row[STATE_COL] == State.DOWNLOADING:
                id_, received = queue.get()
                # FINISHED
                if received == BATCH_FINISISHED:
                    done += 1
                    if done == len(status):
                        row[STATE_COL] = State.FINISHED
                        break
                    else:
                        continue
                elif received == BATCH_ERROR:
                    row[STATE_COL] = State.ERROR
                    break
                status[id_][2] += received
                conf_count += 1
                if conf_count > THRESHOLD_TO_FLUSH:
                    with open(conf_filepath, 'w') as fh:
                        fh.write(json.dumps(status))
                    conf_count = 0
                received_total = sum(t[2] for t in status)
                self.emit('received', row[FSID_COL], received_total)
                #self.emit('received', row[FSID_COL], received)
        except Exception as e:
            print(e)
            for task in tasks:
                task.stop()
            row[STATE_COL] = State.ERROR
        fh.close()
        with open(conf_filepath, 'w') as fh:
            fh.write(json.dumps(status))

        for task in tasks:
            if not task.isAlive():
                task.stop()

        if row[STATE_COL] == State.CANCELED:
            os.remove(tmp_filepah)
            if os.path.exists(conf_filepath):
                os.remove(conf_filepath)
        elif row[STATE_COL] == State.FINISHED:
            self.emit('downloaded', row[FSID_COL])
            os.rename(tmp_filepath, filepath)
            if os.path.exists(conf_filepath):
                os.remove(conf_filepath)

    def destroy(self):
        '''自毁'''
        self.pause()

    def run(self):
        '''实现了Thread的方法, 线程启动入口'''
        self.download()

    def pause(self):
        '''暂停下载任务'''
        self.row[STATE_COL] = State.PAUSED

    def stop(self):
        '''停止下载, 并删除之前下载的片段'''
        self.row[STATE_COL] = State.CANCELED

GObject.type_register(Downloader)
