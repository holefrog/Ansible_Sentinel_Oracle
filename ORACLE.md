# Oracle Cloud 实例网络与域名配置手册

本手册用于解决甲骨文云（OCI）实例无法连接 SSH、Web 端口不通以及域名解析生效慢等常见故障。

---

## 1. 基础网络链路 (VCN 架构)
**现象**：即便开启了防火墙，SSH 依然提示 `Connection timed out`。
**原因**：VCN 缺少互联网网关 (IGW) 或路由表 (Route Table) 指向错误。

### 配置步骤：
1. **创建网关**：进入 `Networking` -> `Internet Gateways` -> `Create Internet Gateway`。
2. **配置路由**：进入 `Route Tables` -> `Default Route Table` -> `Add Route Rules`：
   - **Target Type**: `Internet Gateway`
   - **Destination CIDR**: `0.0.0.0/0`
   - **Target Internet Gateway**: 选择刚才创建的网关。
   > **原理**：这是为服务器指明通往互联网的“大门”。

---

## 2. SSH 身份验证与安全
**现象**：报错 `Permissions for 'xxx.key' are too open` 或 `Permission denied (publickey)`。

### 私钥权限修复 (Key 权限太宽)：
SSH 要求私钥文件必须是私有的。如果权限太开放（如 644 或 777），连接会被拒绝。
```bash
# 修改私钥权限为仅所有者可读
chmod 400 ssh-key-2026-04-30.key
```

### 默认用户名提醒：
- **Ubuntu**: `ubuntu`
- **Oracle Linux / Centos**: `opc`
- **Debian**: `admin`

---

## 3. 云端防火墙 (Security List) 
**操作路径**：`Networking` -> `Security Lists` -> `Default Security List`。

### 端口配置规范：
| 服务 | 协议 | 源 CIDR | 源端口范围 | 目标端口范围 |
| :--- | :--- | :--- | :--- | :--- |
| **SSH** | TCP | `0.0.0.0/0` | **留空 (All)** | `22` |
| **HTTP** | TCP | `0.0.0.0/0` | **留空 (All)** | `80` |
| **HTTPS**| TCP | `0.0.0.0/0` | **留空 (All)** | `443` |

**⚠️ 避坑指南**：**Source Port Range** 必须保持**留空**。如果错误填写为 80/443，会导致正常访问被拦截。

---

## 4. 实例系统内部防火墙
**注意**：云平台放行后，Linux 系统内部（iptables）可能仍有拦截。

### 快速放行命令 (以放行 80 为例)：
- **Ubuntu**: 
  ```bash
  sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
  sudo netfilter-persistent save
  ```
- **Oracle Linux**: 
  ```bash
  sudo firewall-cmd --permanent --add-service=http
  sudo firewall-cmd --reload
  ```

---

## 5. 域名解析生效排查
**诊断命令**：
1. **查权威节点**：`nslookup <域名> 8.8.8.8` (若返回新 IP 则云端已同步)。
2. **清理本地缓存**：执行 `ipconfig /flushdns` (Windows) 或重启浏览器。
3. **CDN 检查**：若使用 Cloudflare，请确认其控制台解析记录已同步更新。



## 一定要重启
**防火墙或者网络设置改变后，一定要重启才会生效**
