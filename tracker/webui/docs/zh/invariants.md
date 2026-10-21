# 数字之间的勾稽关系

一家公司拥有若干园区；园区按楼栋和分期（即 *tranche*，分期）建设；每一层都有规划容量和运行容量。以下是追踪系统要求这些数字遵守的关系 —— 即勾稽关系 —— 每次运行 `tracker invariants` 都会在整个数据库上逐条核对。

每条关系属于以下两类之一：

- **Held**（已保证）—— 系统在写入每一行时强制执行。所有页面（项目详情页、公司页面、[Capex](capex)、地图、API）读到的数字都遵守它。一旦被打破，说明是系统本身的错误，而不是数据的问题；每次发布前测试都会核对这些关系。
- **Reported**（待闭合）—— 已发布的来源还无法让它闭合，系统会指出具体在哪里。其中大多数源于同一个缺口：一个园区先作为整体被描述一次，又按楼栋逐一描述一次，却没有任何信息说明哪栋楼属于哪一部分。自 2026 年 10 月 22 日起，各分期按其构成的层级树相加：标签本身就能确定的 —— 同一楼栋的多个名称、来源点名的上级分期、各部分能装进的园区整体分期 —— 每次读取时都会自动确定；其余的由模型每晚依据原句审阅裁定，或由人员裁定。每一项裁定都会让一个园区移出本清单。

## 已保证

| 关系 | Relationship |
| --- | --- |
| 园区运行容量不超过规划容量。两者矛盾时，由证据更充分的数字判断哪一个描述的是别的东西。参见[数字与日期](numbers-and-dates)。 | A campus's running capacity is never more than its planned capacity. |
| 园区各分项容量之和（按分期引用、待确认、未对应设施、尚未细分，减去重复计算）等于园区总容量。为园区供电的公用事业设施在总数旁单列，永远不计入其中。 | A campus's capacity lines add up to its total; the utility's plant is named beside it, never inside. |
| 园区正在交付的容量不超过园区总容量。 | What a campus delivers is never more than its total. |
| 公司容量等于其名下各园区之和：每个园区只计一次，且在各买方之间完整分配。如果园区的分期点名了买方、却把园区描述了不止一次：只有一个买方时，该买方获得整个园区，不多计；有多个买方时，无法区分各自的部分，园区按整体计入。 | A company's capacity is the sum of the campuses credited to it, each counted once and in full. |

自 2026 年 10 月 22 日起，前三条还会在每次写入时核对。会打破其中任何一条的写入将被拒绝：这篇文章的记录不会保存，夜间日志会点名记录，文章留待错误修复后重新读取。因此，任何页面都不会显示打破已保证关系的园区，哪怕只有一晚。

## 待闭合

| 关系 | Relationship | 打破时意味着什么 |
| --- | --- | --- |
| 任何分期都不大于其所属园区。 | No tranche is larger than its campus. | 几乎总是把千瓦读成了兆瓦。已从所有数字中剔除。 |
| 园区的楼栋与分期只描述园区一次，其容量之和不超过园区总容量。 | A campus's tranches describe it once. | 园区和其中的楼栋被并列为不同的分期，或同一栋楼有两个名字，且尚无规则或裁定将其归位。在[项目详情页](project-page)上显示为 *counted twice over*（被重复计算）。 |
| 每个分期都属于园区内一个具名设施。 | Every tranche belongs to a named facility. | 只写"二期"，却没说是哪栋楼的二期。 |
| 已投运分期的容量之和不超过园区运行容量。 | Live tranches add up to no more than the running capacity. | 同样是重复描述，体现在运行容量上。 |
| 园区所处阶段不早于其已投运的分期。 | A campus is not behind its running tranches. | 阶段落后于分期的实际进展。 |
| 园区宣布时间早于预计上线时间。 | A campus is announced before it is expected online. | 通常是找到的最早文章晚于真正的宣布时间 —— 已有的场址在扩建时才首次被报道。 |

某条关系被打破，本身不会改变任何数字。每个页面上的数字都已遵守上述规则 —— 量级不符的分期被剔除，重复描述只计一次 —— 这份报告列出的是仍待厘清的地方。

园区自己的页面会在 *Capacity by tranche*（分期容量）之下，以 **N relationships not yet closed**（N 条关系尚未闭合）列出它打破的关系 —— 每条关系用一句话说明，并指出在该园区是什么打破了它（参见[项目详情页](project-page)）。

## 逐夜记录

每晚数据核对之后，夜间流程会统计每条关系被多少个园区打破、涉及多少兆瓦，晨报把它与前一晚并列：*tranches_once 113 (80,410 MW) -2, -1,200 MW*，或 *unchanged*（无变化）。数字下降，说明裁定在生效；数字上升，说明有新文章又描述了某个园区一次，第二天晚上的审阅会处理它。
