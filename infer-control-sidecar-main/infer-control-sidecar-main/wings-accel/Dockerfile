FROM alpine:3.18

# 设置工作目录
WORKDIR /accel

# 复制 accel 文件到镜像中
COPY . /accel/

# 设置权限
RUN chmod +x /accel/install.sh
RUN chmod +x /accel/wings_engine_patch/install.sh

# 默认命令
CMD ["/bin/sh"]
