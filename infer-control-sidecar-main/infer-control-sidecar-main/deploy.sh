#!/bin/bash

# Wings-Infer K8s 部署脚本

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 打印函数
print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查kubectl
check_kubectl() {
    if ! command -v kubectl &> /dev/null; then
        print_error "kubectl not found. Please install kubectl first."
        exit 1
    fi
    print_info "kubectl found: $(kubectl version --client --short)"
}

# 检查集群连接
check_cluster() {
    if ! kubectl cluster-info &> /dev/null; then
        print_error "Cannot connect to Kubernetes cluster."
        exit 1
    fi
    print_info "Kubernetes cluster connection verified"
}

# 构建镜像
build_image() {
    print_info "Building Wings-Infer Docker image..."
    docker build -t wings-infer:latest .
    if [ $? -eq 0 ]; then
        print_info "Docker image built successfully"
    else
        print_error "Failed to build Docker image"
        exit 1
    fi
}

# 部署应用
deploy() {
    ENGINE_TYPE=${1:-vllm}

    print_info "Deploying Wings-Infer with $ENGINE_TYPE engine..."

    if [ "$ENGINE_TYPE" = "sglang" ]; then
        kubectl apply -f k8s/deployment-sglang.yaml
    else
        kubectl apply -f k8s/deployment.yaml
    fi

    kubectl apply -f k8s/service.yaml

    print_info "Deployment completed"
}

# 查看状态
status() {
    print_info "Checking deployment status..."
    echo ""
    kubectl get pods -l app=wings-infer
    echo ""
    kubectl get svc wings-infer-service
}

# 查看日志
logs() {
    CONTAINER=${1:-wings-infer}
    print_info "Showing logs for container: $CONTAINER"
    kubectl logs -f deployment/wings-infer -c $CONTAINER
}

# 删除部署
delete() {
    print_warn "Deleting Wings-Infer deployment..."
    kubectl delete -f k8s/service.yaml --ignore-not-found=true
    kubectl delete -f k8s/deployment.yaml --ignore-not-found=true
    kubectl delete -f k8s/deployment-sglang.yaml --ignore-not-found=true
    print_info "Deployment deleted"
}

# 端口转发
port_forward() {
    LOCAL_PORT=${1:-9000}
    print_info "Setting up port forwarding..."
    kubectl port-forward service/wings-infer-service $LOCAL_PORT:9000
}

# 获取服务URL
get_url() {
    SERVICE=$(kubectl get svc wings-infer-service -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
    if [ -z "$SERVICE" ] || [ "$SERVICE" = "<pending>" ]; then
        # 如果LoadBalancer不可用，使用NodePort
        NODE_PORT=$(kubectl get svc wings-infer-service -o jsonpath='{.spec.ports[0].nodePort}')
        NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="ExternalIP")].address}')
        if [ -z "$NODE_IP" ]; then
            NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
        fi
        if [ -n "$NODE_PORT" ]; then
            echo "http://$NODE_IP:$NODE_PORT"
        else
            print_error "Cannot determine service URL. Use port-forward instead."
            echo "Run: ./deploy.sh forward"
        fi
    else
        echo "http://$SERVICE:9000"
    fi
}

# 测试API
test_api() {
    URL=$(get_url)
    print_info "Testing API at: $URL"

    echo ""
    print_info "Testing health endpoint..."
    curl -s $URL/health | jq .

    echo ""
    print_info "Testing completion endpoint..."
    curl -s -X POST $URL/v1/completions \
        -H "Content-Type: application/json" \
        -d '{
            "prompt": "你好，我今天很开心!",
            "max_tokens": 20,
            "temperature": 0.7
        }' | jq .
}

# 显示帮助
show_help() {
    cat << EOF
Wings-Infer K8s 部署脚本

用法: ./deploy.sh [命令] [选项]

命令:
    build           构建 Docker 镜像
    deploy [engine] 部署应用 (engine: vllm 或 sglang，默认: vllm)
    status          查看部署状态
    logs [container] 查看日志 (container: wings-infer 或 vllm-engine)
    delete          删除部署
    forward [port]  端口转发 (默认: 9000)
    url             获取服务URL
    test            测试API
    help            显示帮助信息

示例:
    ./deploy.sh build              # 构建镜像
    ./deploy.sh deploy             # 部署vLLM版本
    ./deploy.sh deploy sglang      # 部署SGLang版本
    ./deploy.sh status             # 查看状态
    ./deploy.sh logs wings-infer   # 查看wings-infer日志
    ./deploy.sh forward            # 端口转发
    ./deploy.sh test               # 测试API
    ./deploy.sh delete             # 删除部署

EOF
}

# 主函数
main() {
    check_kubectl
    check_cluster

    case "${1:-help}" in
        build)
            build_image
            ;;
        deploy)
            #build_image
            deploy "$2"
            status
            ;;
        status)
            status
            ;;
        logs)
            logs "$2"
            ;;
        delete)
            delete
            ;;
        forward)
            port_forward "$2"
            ;;
        url)
            get_url
            ;;
        test)
            test_api
            ;;
        help|--help|-h)
            show_help
            ;;
        *)
            print_error "Unknown command: $1"
            show_help
            exit 1
            ;;
    esac
}

# 运行主函数
main "$@"
