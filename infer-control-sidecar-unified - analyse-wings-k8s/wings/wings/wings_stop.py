# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
Wings Inference Service Stop Tool

Stops running inference services by reading PID files
"""

import os
import sys
import signal
import logging
import argparse


def stop_service(pid_file: str):
    """
    Stop service based on PID file

    Args:
        pid_file: Path to file containing process PID
    """
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger(__name__)
    
    # 如果是mindie服务，使用pkill强制停止所有相关进程
    if 'mindie' in pid_file.lower():
        logger.info("Detected mindie service, using force stop method")
        try:
            os.system("/usr/bin/pkill -9 -f 'mindie'")
            logger.info("Force stopped all mindie related processes")
            return
        except Exception as e:
            logger.error(f"Error while force stopping mindie processes: {e}")
            sys.exit(1)
    
    try:
        # 读取PID文件
        with open(pid_file, 'r') as f:
            lines = f.readlines()
            parent_pid = int(lines[0].strip().split(':')[1])
            child_pid = int(lines[1].strip().split(':')[1])
        
        logger.info(f"Stopping service - Parent PID: {parent_pid}, Child PID: {child_pid}")
        
        # 先停止子进程
        try:
            os.kill(child_pid, signal.SIGTERM)
            logger.info(f"Sent stop signal to child process {child_pid}")
        except ProcessLookupError:
            logger.warning(f"Child process {child_pid} does not exist")
            
        # 再停止父进程
        try:
            os.kill(parent_pid, signal.SIGTERM)
            logger.info(f"Sent stop signal to parent process {parent_pid}")
        except ProcessLookupError:
            logger.warning(f"Parent process {parent_pid} does not exist")
        
    except FileNotFoundError:
        logger.error(f"PID file {pid_file} does not exist")
        sys.exit(1)
    except ProcessLookupError:
        logger.error(f"Process {parent_pid} does not exist or has terminated")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error occurred while stopping service: {e}")
        sys.exit(1)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Stop Wings inference service')
    parser.add_argument(
        '--pid-file', 
        type=str,
        default='vllm_pid.txt',
        help='PID file path (default: vllm_pid.txt)'
    )
    
    args = parser.parse_args()
    stop_service(args.pid_file)