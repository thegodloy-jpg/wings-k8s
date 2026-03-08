# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

import os
import subprocess
import threading
import time
import logging
from typing import Union

from wings.utils.file_utils import safe_write_file

logger = logging.getLogger(__name__)

root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LOG_DIR = os.path.join(root_dir, "wings", 'logs')


def wait_for_process_startup(
    process: subprocess.Popen,
    success_message: str,
    _logger: logging.Logger = None
) -> bool:
    """
    等待进程启动并检测特定成功消息
    
    Args:
        process: 要监控的子进程
        success_message: 表示启动成功的日志消息
        timeout: 超时时间(秒)，None表示无限等待，默认30分钟
        logger: 日志记录器
    
    Returns:
        bool: 是否成功检测到启动消息
    """
    if _logger is None:
        _logger = logger
    
    started = threading.Event()

    def _log_stream(stream, level):
        for line in stream:
            if line.strip():
                _logger.log(level, line.strip())
                if success_message in line.strip():
                    started.set()

    # 启动日志线程
    stdout_thread = threading.Thread(
        target=_log_stream,
        args=(process.stdout, logging.INFO),
        daemon=True
    )
    stderr_thread = threading.Thread(
        target=_log_stream,
        args=(process.stderr, logging.INFO),
        daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()

    # 等待进程结束或检测到启动成功
    while True:
        if started.is_set():
            _logger.info(f"Detected service startup success message: {success_message}")
            return True

        if process.poll() is not None:  # 进程已结束
            if process.returncode != 0:
                raise RuntimeError(f"Process startup failed with return code: {process.returncode}")
            return False

        time.sleep(1)


def log_process_pid(
    name: str,
    parent_pid: Union[int, None] = None,
    child_pid: Union[int, None] = None,
    log_dir: str = _LOG_DIR
) -> None:
    """
    记录进程PID到日志文件
    
    Args:
        name: 进程名称标识
        parent_pid: 父进程PID (可选)
        child_pid: 子进程PID (可选)
        log_dir: 日志目录路径 (默认: wings.logs)
    """
    try:
        # 确保日志目录存在
        os.makedirs(log_dir, exist_ok=True)
        
        # 生成日志文件名
        pid_file = os.path.join(log_dir, f"{name}_pid.txt")
        
        # 写入PID信息
        pid_content = ""
        if parent_pid is not None:
            pid_content += f"parent:{parent_pid}\n"
        if child_pid is not None:
            pid_content += f"child:{child_pid}\n"
        safe_write_file(pid_file, pid_content)
        
        # 记录日志
        log_msg = f"Logged process PID - name: {name}"
        if parent_pid is not None:
            log_msg += f", parent: {parent_pid}"
        if child_pid is not None:
            log_msg += f", child: {child_pid}"
        log_msg += f" to {pid_file}"
        
        logger.info(log_msg)
    except Exception as e:
        logger.error(f"Failed to log PID: {e}", exc_info=True)
        raise


def log_stream(process):
    def _log_stream():
        try:
            while process.poll() is None:
                # 使用readline确保实时输出
                stdout_line = process.stdout.readline()
                if stdout_line:
                    logger.info(stdout_line.strip())
                
                stderr_line = process.stderr.readline()
                if stderr_line:
                    logger.error(stderr_line.strip())
            
            # 进程结束后读取剩余输出
            for line in process.stdout:
                if line.strip():
                    logger.info(line.strip())
            for line in process.stderr:
                if line.strip():
                    logger.error(line.strip())
        except Exception as e:
            logger.error(f"Log stream error: {e}")
        finally:
            logger.info("Service log stream ended")

    log_thread = threading.Thread(target=_log_stream, daemon=False)
    log_thread.start()
    
    logger.info("Service started successfully. Process and log thread are running independently.")