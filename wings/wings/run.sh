docker run -d --shm-size=512g \
  --name AIspaceWings_test_zhanghui_260122\
  --device=/dev/davinci_manager \
  --device=/dev/hisi_hdc \
  --device=/dev/devmm_svm \
  --device=/dev/davinci0\
  -v /usr/local/Ascend/driver:/usr/local/Ascend/driver:ro \
  -v /usr/local/sbin:/usr/local/sbin:ro \
  -v /data/nvme1n1/models/Qwen3-8B:/weights\
  -v /nfs_models/zh/project:/opt/wings\
  -v /var/log:/var/log \
  wings-npu-aarch64:25.0.1 \
  tail -f /dev/null



python run_batch_test.py --config llm_batch_perf_test_config.json

bash /opt/wings/wings_start.sh --model-name Qwen3-8B --model-path /weights   --engine mindie  --input-length 8192  --output-length 8192