# 认证与HTTP接口

启动：`python -m uvicorn api:app_factory --factory --host 127.0.0.1 --port 8000`。交互文档在`http://127.0.0.1:8000/docs`。

注册与登录接受JSON的`username/password`，用户名3至40位字母数字下划线，密码12至128位。`POST /auth/register`创建账号，`POST /auth/login`返回1小时有效的Bearer令牌，后续放入`Authorization: Bearer ...`。`POST /auth/logout`撤销当前令牌。密码使用Argon2，数据库仅存令牌SHA-256摘要；原始密码和令牌不写入行程记录。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | /health | 进程健康 |
| GET | /me | 当前用户ID |
| GET | /conversations | 当前用户会话 |
| POST | /plans | 创建行程：query与request（TripRequest） |
| GET | /conversations/{id} | 当前有效版本 |
| GET | /conversations/{id}/versions/{version} | 指定历史版本 |
| GET | /conversations/{id}/history | 消息、版本与执行报告 |
| POST | /conversations/{id}/revisions | instruction、expected_version、request_updates、preserve_hotels |

认证之外的业务接口必须登录；其他用户的会话统一返回404，并在付费模型调用前拒绝。所有者来自服务端身份，不接受请求体指定。API有登录/注册及用户规划限流，最多同时执行2次规划，繁忙直接503；这些是单进程保护，尚未实现跨进程Redis限流。规划错误映射为422、409、429、503或504，不返回SDK原始异常。

Streamlit默认需要注册登录，并隔离历史和修改。退出登录会清空当前页面状态。原有本地记录迁移后标为`local`，不分配给新账号；只有启动服务的操作者设置`TRAVEL_LOCAL_MODE=1`时才显示这些记录。该模式仅用于可信本机运行，API始终要求认证。原CLI也是可信本机入口。

当前没有密码找回、管理员后台或邮件验证。对外部署必须配置HTTPS；不要把本机模式页面暴露到公网。认证实现参考[FastAPI官方安全教程](https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/)，这里使用数据库可撤销会话而非JWT。
