# Android 开机自启服务

服务文件：`init/vendor.logagent.rc`。服务名为 `vendor.logagent`，可执行程序固定为 `/vendor/bin/device-agent`，配置目录固定为 `/data/vendor/logagent/`。

开机完成（`sys.boot_completed=1`）后自动启动，使用 system 用户和 system、inet 用户组，异常退出后由 init 重启，重启间隔配置为 10 秒。程序持续前台运行，由 init 管理，不使用 nohup 或 shell 后台符号。

## 固件集成

将程序和 rc 文件安装到：

```text
bin/device-agent                 -> /vendor/bin/device-agent                  0755
init/vendor.logagent.rc           -> /vendor/etc/init/vendor.logagent.rc       0644
```

把 `sepolicy/vendor_logagent.te`、`sepolicy/file_contexts` 合入设备的 vendor SELinux 策略目录并编译固件。可执行文件需要标签 `u:object_r:vendor_logagent_exec:s0`，启动后应进入 `u:r:vendor_logagent:s0` 域。单纯把 .te 和 file_contexts 文本复制到运行中的设备不会加载策略。

配置、CA 证书和现有 Token 部署到数据分区，不能随每次开机覆盖或删除。真实设备凭证不要写入公共固件模板。

## 从手动运行迁移

安装前先停止当前手动启动的 Agent，避免同一设备码产生两个连接。下面的命令在 Android 的 root 终端中执行。先确认二进制及 rc 文件已安装，且固件已具备对应 SELinux 策略。

```sh
mkdir -p /data/vendor/logagent/run/cloud-agent/tokens
chown system:system /data/vendor/logagent
chmod 700 /data/vendor/logagent
chown system:system /data/vendor/logagent/device-agent.conf
chown system:system /data/vendor/logagent/device-agent.cloud.conf
chown system:system /data/vendor/logagent/log-sources.json
chown system:system /data/vendor/logagent/ca-bundle.pem
chmod 600 /data/vendor/logagent/device-agent.cloud.conf

# 只调整 Agent 自身的运行状态目录，保留现有 Token 和命令结果。
chown -R system:system /data/vendor/logagent/run
chmod 700 /data/vendor/logagent/run/cloud-agent/tokens
find /data/vendor/logagent/run/cloud-agent/tokens -type f -exec chmod 600 {} \;

chmod 755 /vendor/bin/device-agent
chmod 644 /vendor/etc/init/vendor.logagent.rc
restorecon /vendor/bin/device-agent /vendor/etc/init/vendor.logagent.rc
restorecon -RF /data/vendor/logagent
```

业务日志需要另外确保 system 用户有读取权限，同时保留业务写入进程自己的写权限。不要直接递归修改其他业务的日志目录属主。

## 当前调试设备安装 rc

电脑端可先把 rc 放到设备临时目录：

```text
adb push out/android-arm64/init/vendor.logagent.rc /data/local/tmp/vendor.logagent.rc
```

在 `/vendor` 已可写且策略已合入的调试设备上，以 root 执行：

```sh
cp /data/vendor/logagent/device-agent /vendor/bin/device-agent
cp /data/local/tmp/vendor.logagent.rc /vendor/etc/init/vendor.logagent.rc
```

然后完成上一节的权限调整，再重启设备。Android init 会在启动时加载 vendor 分区的 rc 文件；运行时新复制的 rc 不能直接靠 start 命令注册为新服务。生产固件应通过固件构建安装这些文件。

## 重启验证与控制

```sh
reboot
```

重新连接 Android 后检查：

```sh
getprop init.svc.vendor.logagent
ps -AZ | grep device-agent
logcat -s DeviceAgent:I
```

状态应为 `running`，进程域应为 `vendor_logagent`，日志应最终出现 `READY hello handshake complete`。`running` 仅表示进程存活，不代表云端连接已经成功。

使用 root 终端控制已注册的服务：

```sh
# 停止（本次启动中保持停止，除非再次 start 或重启设备）。
setprop ctl.stop vendor.logagent
# 启动。
setprop ctl.start vendor.logagent
# 修改配置后重启。
setprop ctl.restart vendor.logagent
```

如果没有服务状态，先检查 rc 是否安装到正确目录并已重启；如果反复 restarting 或启动失败，检查 init 日志及 `avc: denied`。手动运行成功不代表 init 的 system 用户和专用 SELinux 域具有同样的权限。
