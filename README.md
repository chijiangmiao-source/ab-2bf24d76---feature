# 束流整形磁铁垫片校正复核

更换束流整形磁铁垫片后，多处机械校正量须同时满足测得的耦合位移关系，
且每项调整只能取整数个最小垫片单位。本应用将该校核建模为整数丢番图
方程组 `A·x = b`，以**任意精度整数**精确判定可解性：

- 可解时给出精确整数校正量，并可点选任一约束查看各项乘积、左侧和及其目标值；
- 在可解结论中可为每个机械校正项填写最小/最大垫片数并发起**行程受限审计**：
  系统从特解与齐次整数解格出发，在边界整数格点中全局枚举，返回相对当前精确
  校正量总绝对偏移最小、并按变量标识顺序的偏移向量字典序稳定裁决的一组调整，
  逐项展示边界、偏移与每条原始约束的精确复算；不取整连续解、不沿单个自由方向贪心；
- 无解时展示由可逆整数行变换（Smith 正规形 `U·A·V = D`）导出的**规范除尽障碍**：
  变换后目标 `U·b` 的某一分量不能被对应主元整除（或零行目标非零），
  并列出该行变换与原始目标的逐项乘积来源；
- 全程不将大整数转为浮点数、不对有理解四舍五入；超过 2⁵³ 的系数与目标
  在页面上仍以精确整数文本呈现；
- 前端以世代号 + `AbortController` 保证：发起较大复核或审计后立即编辑草稿、
  修改边界或取消时，过期返回不会覆盖当前草稿的状态或结论。

## 运行

```bash
# 启动页面与健康路径（宿主端口默认 8080，可用 HOST_PORT 配置）
docker compose up app
HOST_PORT=9000 docker compose up app

# 页面:  http://localhost:8080/
# 健康:  http://localhost:8080/health

# 一次性验收（先确认无解证据，再跑测试与构建检查，最后 HTTP 冒烟；退出码即结论）
docker compose up --exit-code-from verify verify
# 或
docker compose run --rm verify
```

## 接口

### `GET /health`
返回 `{"status": "ok"}`。

### `POST /api/review`
请求体（整数请以字符串提交，避免浮点误差；安全范围内的 JSON 整数也可接受，
浮点数一律拒绝）：

```json
{
  "variables": ["K1", "K2", "K3"],
  "matrix": [["9007199254740993", "1", "0"], ["1", "3", "1"], ["0", "2", "4"]],
  "target": ["18014398509481989", "12", "10"]
}
```

可解响应（节选）：`solution` 为精确整数校正量；`constraints[i]` 含每项乘积
`terms[]`、左侧和 `sum` 与目标值 `target`；`homogeneousBasis` 为自由校正方向。

无解响应（节选）：`obstruction` 含 `type`（`non_divisible` / `zero_row`）、
变换后行号 `row`、`pivot`、`transformedTarget`、`remainder` 以及行变换来源
`uRow` / `uRowTerms`；`smith` 给出主元序列与变换后目标向量。

### `POST /api/audit`
在 `/api/review` 的字段之外，额外携带每项一条的 `bounds`（长度与变量数一致；
元素为 `[最小垫片数, 最大垫片数]`，任一侧或整项可为 `null`/空字符串表示不限）：

```json
{
  "variables": ["K1", "K2"],
  "matrix": [["1", "1"]],
  "target": ["5"],
  "bounds": [["0", "4"], ["0", "4"]]
}
```

响应在原始结论字段（`solution` 特解、`homogeneousBasis` 齐次整数解格、
`smith`）之外给出：

- `solvable=false`：原始方程无整数解，`reason="equation_unsolvable"`，
  同时返回 `obstruction` 规范除尽障碍；
- `solvable=true, feasible=false`：`reason="no_bounded_solution"`，方程可解
  但行程内无整数点，`items` 逐项标出当前校正量是否越界；
- `solvable=true, feasible=true`：`adjusted` 为全局最优调整、`offsets` 为
  相对当前特解的带符号偏移、`totalAbsOffset` 为总绝对偏移；`items` 逐项给出
  边界、当前量、调整量、偏移与在界判定；`constraints` 对调整后方案逐条做
  乘积、左侧和与目标值的精确复算；`nodes` 为完备枚举的节点计数。

错误分别说明原因：边界格式非法返回 HTTP 400（如最小值大于最大值、出现浮点/
非整数文本、条数不符）；双侧边界不能张成全部自由方向时返回 HTTP 422，提示
需为足够多行程相互独立的校正项同时填写最小与最大垫片数。

## 本地开发（无 Docker）

```bash
python3 -m unittest discover -s tests -t .   # 测试
python3 -m app.server                         # 启动（PORT 环境变量可改端口）
APP_BASE_URL=http://127.0.0.1:8080 python3 scripts/verify.py  # 验收
```

## 目录

```
app/diophantine.py   精确整数求解器（Smith 正规形，任意精度）
app/bounded.py       行程受限审计（特解+齐次整数解格上的全局最优枚举）
app/server.py        标准库 HTTP 服务（页面、/health、/api/review、/api/audit）
app/static/          复核页面（精确文本渲染、防竞态、边界审计）
tests/               求解器、行程审计与 HTTP 层单元测试
scripts/verify.py    一次性验收例程
Dockerfile           应用镜像
docker-compose.yml   app（可配置宿主端口）+ verify（一次性验收）
```
