source /opt/sglang_env/bin/activate
# 规避faulthandler.enable()执行失败的问题
# 1. 定义目标文件路径（避免重复写路径，便于维护）
TARGET_FILE="/opt/sglang_env/lib/python3.10/site-packages/sglang/srt/managers/scheduler.py"

# 2. 定义“已修改”的特征代码（从修改后的内容中选一段唯一的行）
MODIFIED_MARK="logger.warning(f\"Failed to enable faulthandler: {e}\")"

# 3. 检查文件中是否已存在特征代码（-q 静默模式，只返回存在/不存在的状态）
if ! grep -q "$MODIFIED_MARK" "$TARGET_FILE"; then
    # 4. 不存在特征代码 → 执行 sed 修改（你的原 sed 命令）
    sed -i 's/faulthandler\.enable()/try:\n        faulthandler.enable()\n    except Exception as e:\n        logger.warning(f"Failed to enable faulthandler: {e}")/g' "$TARGET_FILE"
    echo "The file has been modified for the first time：$TARGET_FILE"
else
    # 5. 存在特征代码 → 跳过修改
    echo "The file has been modified, so it will be skipped this time：$TARGET_FILE"
fi