# Strata Vault · 地层档案库

现场记录可以在离线工作站分片制作成档案包，运抵接收站后按所缺分片续传。接收站校验完整档案，再以不可变版本发布，同一字节对象可供多个版本共享。报告引用可以固定具体版本。库房发现介质损坏时可以查出受影响的版本，输入经验证的字节完成修复。示例和测试均使用本地合成记录。

## 运行

使用 Python 3.11 或更高版本，并预装 `requirements.txt` 中的版本。将库目录置于本机可靠的文件系统上。服务默认绑定环回地址，供受信任的接收站与维护人员使用。目录应由服务独占写入。

```bash
python -m strata --data vault-data
python -m strata.field --help
python -m unittest discover -s tests -v
python -m compileall -q strata
```

服务启动后可通过 `/docs` 查看接口、请求结构和参数。所有时刻和期限均使用整数秒，时刻基于 Unix 时间。

## 入库与版本

先向 `POST /collections` 提交 `name` 与 `retention_seconds` 创建馆藏。馆藏初始头版本为 0。现场用 `python -m strata.field capture` 将正常文件目录封装成可搬运的本地档案包。捕获参数包含馆藏名、源端令牌和它所依据的头版本。原目录请保持静止直至捕获完成。可用 `check` 校验现场档案，可在接收站用 `submit` 上传。弱网导致响应丢失时，用同一个档案包重新提交会回到同一会话并查询缺片。

`POST /collections/{collection}/deposits` 接收 `token`、`parent`、`lifetime_seconds` 及 `manifest`。清单版本为 `1`，每个文件包含 `path`、`size`、`sha256` 和按原文件顺序排列的 `parts`。分片由 `sha256` 与 `size` 描述。清单按路径排序后生成 UTF-8 JSON 指纹，文件内分片顺序参与指纹。空文件可以入库。档案最多 500 个文件、8192 个分片引用、4 GiB 逻辑字节，单片最多 16 MiB。文件路径须在不同操作系统上保持唯一，保留 `_vault` 给出库元数据。

逐片以 `application/octet-stream` 上送到 `PUT /deposits/{id}/parts/{digest}`。重复上传保留已有收据，共享字节只落盘一次。将 `POST /deposits/{id}/publish` 作为最后一步。系统会核对全文件摘要，并比较当前头版本与提交时依据的版本。图层两次分别基于同一头版本送审时，只有一次可以成为下一版。发布回执丢失可再次请求同一会话。取消及到期的会话保留原令牌和清单痕迹。

## 更正候选与合订

两组人可能带着同一个历史版本各自去现场，回库时另一组已经先发布。晚到的完整包不应再走“父版本必须等于当前头”的线性存款，而应作为**有独立身份和期限的更正候选**接入，再由档案员依据两组相对共同基础版的明确变化形成**合订版**。原有线性存款、发布与历史版本合同保持不变。

- `POST /collections/{collection}/candidates`（近义路径 `/corrections`）接收 `token`、`parent`（共同历史版本，可为 0）、`lifetime_seconds`、完整 `manifest`，以及可选的 `changes`/`moves`。同一 `(collection,token)` 以相同绑定重放会回到同一候选并给出缺片。分片送到 `PUT /candidates/{id}/parts/{digest}`，续传、去重收据与存款一致；现场可用 `python -m strata.field submit <parcel> --vault-url … --as-candidate --changes-file moves.json` 继续补送。
- **移动只承认明确记录**：`changes` 形如 `[{"op":"move","from":"旧路径","to":"新路径"}]`（兼容 `rename`、`old_path/new_path` 等写法）。来源必须在基础版中、目标必须在候选清单中，且移动后旧路径不能仍留在候选里。系统**不会**因为摘要相同或文件名相同就把“消失的旧路径 + 新增的相似路径”配对成移动。新增、删除、同路径改内容由三份清单确定性得出。
- `GET /candidates/{id}/plan`（或 `POST …/plan`、`…/conflicts`）返回基础版/当前头/候选的摘要绑定、两侧明确变化（`head_changes`、`candidate_changes`）、预合并条目 `entries` 与冲突。合并按文件身份区分路径维度与内容维度：一侧明确移动、另一侧修改内容且无意图冲突时，修改会落到移动后的条目。
- 无法自动判断的情形进入 `conflicts`：`path_conflict`（双方把不同资料安排到同一归档位置，含不同身份移入同一路径）、`delete_modify`（一边删除、另一边保留改动；删除对移动也归入此类复核）、`content_conflict`、`move_conflict`。每个冲突都带 `key`、双方/基础来源的路径、摘要、大小与可取回链接（`sides`、`sources`、`hrefs`）。`GET /review-queue` 或 `GET /collections/{c}/review-queue` 给档案员看待决来源。
- `POST /candidates/{id}/decisions` 提交决定，例如 `{"key":"<conflict key>","choice":"head|candidate"}`；删除/修改冲突还可用 `"delete"|"keep"`，位置冲突可用额外 `target_path` 同时保留两份。也支持 `decide(id, key, choice)`、`decide(id, "keep")` 等服务调用形式。决定绑定到 `context_sha256`（候选、基础版摘要、当前头号与摘要）和 `conflict_sha256`。
- 头版本在决定之后再次前进会改变合并上下文（`context_sha256` 绑定候选、基础版摘要、当前头号与摘要）。既有决定不会被默默用于新合并：计划把它们标成 `stale` 并保留在 `review_history`，直接发布会返回带 `review_required`/`decisions_stale` 的未决冲突。档案员可用 `POST /candidates/{id}/reconfirm` 显式复核——它只把**冲突指纹未变**（`unchanged_conflict=true`，即新版未实际影响该变化）的决定重申到当前上下文；指纹已变的受影响冲突仍须重新提交选择。所有历史决定始终可查。
- `POST /candidates/{id}/publish-merge`（近义 `…/merge`、`…/publish`，或 `POST /collections/{c}/merges/publish`）在以下条件全部满足时，把合订版作为当前头的下一普通版本发布（`parent` 为当前头，版本链、审计、取回合同与普通版本完全一致）：候选仍在有效期且不缺片；当前头仍是候选基础版的可验证后代（可传 `expected_head` 做乐观校验）；每个当前冲突都有绑定当前上下文的决定；合订清单的全部分片与整文件摘要通过验证。重复发布返回同一版本。合订记录可经 `GET /collections/{c}/merges`、`GET /candidates/{id}/merge` 查询，候选详情与发布结果都带 `retrieval.file`/`retrieval.package` 取回地址。
- 候选在有效期内通过两类目录声明保护字节：已确认接收的新片（`candidate_received_parts`）与候选依赖的基础历史版全部分片（`candidate_dependencies`）。因此馆藏头更新、基础版退役（退役后只要候选仍依赖，旧版文件/整包仍可取回；候选声明释放后恢复退役 410 合同）或另一候选撤回，都不会让仍被依赖的字节被标记/回收。`DELETE /candidates/{id}` 撤销、`POST /candidates/{id}/close` 结束（近义 `cancel/withdraw`、`finish/conclude`），或由 `POST /operations/expire` 按独立 `expires_at` 到期，都会删除该候选自己的保护声明；之后 `mark`/`sweep` 才可回收再无任何持有者的字节。

## 取回与维护

每个历史版本可按馆藏、版本号和清单摘要引用。可用 `GET /collections/{collection}/revisions/{number}/file?path=…` 取回单文件，支持单个字节范围。服务先核对组成该文件的全部字节，再发送成功响应。完整 ZIP 可通过同一路径下的 `package` 取回，附 `_vault/manifest.json` 和 `_vault/receipt.json`。`python -m strata.field verify` 需要已知的馆藏、版本和清单摘要，支持在验证成功后还原到新目录。外部传来的 ZIP 应使用已可信保存的引用核对。

- `coverage` 显示各文件受已知介质问题的影响。取回未涉及损坏对象的文件仍可继续。
- `holds/{reference}` 支持固定与解除版本。头版本、仍在保留期的版本和引用未解除的版本会阻止退役，退役保留版本清单和事件。
- `/operations/scan` 分页重新读取介质。事件编号用于把修复请求绑定到仍有效的损坏记录。一次修复会恢复该对象被多个版本共享的字节。
- `/operations/forecast` 与馆藏的 `footprint` 分别显示全库待回收对象和馆藏的保护占用。先执行 `expire` 同时释放已到期存款会话与更正候选的收据/依赖声明（响应中的 `expired` 为存款、`candidates` 为候选），再用 `mark` 和 `sweep` 回收。已标记对象会暂停接收新收据，回收进程重启后可继续。
- `/operations/reconcile` 可核对未入目录的物理文件，默认只读。`/operations/staging-cleanup` 清理中断写入的临时文件。
- `/operations/audit` 检查目录中的版本链、保留、清单和收据关系。介质字节另由扫描验证。`/events` 可查库级事件或按馆藏过滤。

大文件取回时，服务在同一次目录事务中核验并准备响应，完成后释放写锁并送出数据。准备过程会占用本机暂存空间。业务繁忙时客户端可在收到临时繁忙响应后按原令牌重试。
