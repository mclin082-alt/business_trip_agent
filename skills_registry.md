# Skill Registry

当前生效 Skill 数：13（另有 1 个未接入运行时的版本演进草案，见文末）

| Skill | 版本 | 允许角色 | 外部副作用 | 风险等级 | 输出字段（成功态） | 失败语义 |
|---|---|---|---|---|---|---|
| `get_customer_info` | v1 | intern, employee, manager, travel_admin | 否 | low | status: string; customer_id: string; company_id: string; company: string; address: string; contact: object; note: string | 只读查询，失败可直接重试或换参数重新查询，不影响任何已产生的状态。 |
| `get_my_calendar` | v2 | intern, employee, manager, travel_admin | 否 | low | status: string; date: string; events: array; free_slots: array; source: string; trace_id: string; data_as_of: string/null; latency_ms: number | 只读查询，不产生任何外部写操作。temporarily_unavailable 可有限重试；permission_denied 应提示用户完成授权；data_missing 代表未知，绝不能当作全天有空；invalid_input 应要求用户确认日期。 |
| `get_customer_calendar` | v1 | intern, employee, manager, travel_admin | 否 | low | status: string; date: string; events: array; free_slots: array | 只读查询，失败可直接重试或换参数重新查询，不影响任何已产生的状态。 |
| `get_travel_policy` | v1 | intern, employee, manager, travel_admin | 否 | low | status: string; flight_budget: number; hotel_budget_per_night: number; notes: string; policy_refs: object | 只读查询，失败可直接重试或换参数重新查询，不影响任何已产生的状态。 |
| `search_flight` | v1 | intern, employee, manager, travel_admin | 否 | low | status: string; flights: array | 只读查询，失败可直接重试或换参数重新查询，不影响任何已产生的状态。 |
| `search_hotel` | v1 | intern, employee, manager, travel_admin | 否 | low | status: string; options: array | 只读查询，失败可直接重试或换参数重新查询，不影响任何已产生的状态。 |
| `delegate_booking_plan` | v1 | intern, employee, manager, travel_admin | 否 | low | status: string; proposal_id: string; delegation_id: string; booking_type: string; recommended_option_id: string; rationale: string; alternatives_considered: array; risk_flags: array | missing_context 表示尚未搜索候选，应先调用对应 search Skill；delegation_failed 表示子 agent 输出未通过 Schema 或候选 ID 校验，可修正 objective/constraints 后重试一次。 |
| `delegate_policy_review` | v1 | intern, employee, manager, travel_admin | 否 | medium | status: string; review_id: string; proposal_id: string; model_decision: string; runtime_decision: string; effective_decision: string; conflict: boolean; policy_ids: array | not_found 表示 proposal_id 无效；delegation_failed 表示复核输出未通过 Schema。没有成功 review 时，book Skill 会 fail closed。 |
| `book_flight` | v2 | employee, manager, travel_admin | 是 | medium | status: string; flight_id: string; departure_time: string; arrival_time: string; price: number; order_id: string; message: string; duplicate_call: boolean | 预订失败（如实时售罄）不等于任务失败：应从已知候选中重新选择或重新搜索，但同一 flight_id 不应连续重试；重复调用同一 flight_id 的成功预订会被幂等处理，不会产生第二笔订单。 |
| `book_hotel` | v2 | employee, manager, travel_admin | 是 | high | status: string; hotel_id: string; hotel_name: string; price_per_night: number; confirmation_id: string; message: string; duplicate_call: boolean | 超出政策预算且未获批准会被运行时硬拦截（status=rejected），不会静默放行，且拦截理由会带上依据的具体 policy_id/version（来自 policy_store，与 get_travel_policy 返回给模型看的政策原文同源，不是裸数字）；拦截后应引导 Agent 调用 request_human_approval 而不是重试同一超预算酒店。重复调用同一 hotel_id 的成功预订会被幂等处理。 |
| `sync_calendar` | v1 | employee, manager, travel_admin | 是 | low | status: string; event_id: string | 失败后可直接重试，日历事件创建本身可幂等重放，不涉及不可逆的外部动作。 |
| `send_confirmation_email` | v1 | employee, manager, travel_admin | 是 | high | status: string; message_id: string | 这是面向客户的不可逆外部动作，失败后不能静默重试或换措辞硬发；应重新走一次 request_human_approval 确认之后再决定是否重发。 |
| `request_human_approval` | v1 | employee, manager, travel_admin | 否 | low | status: string; message: string | 调用本身不会失败；用户拒绝不是错误，是正常分支，Agent 应据此重新规划而不是重试原方案。无论批准还是拒绝，运行时都会在 approval_records 里连带记录本次审批依据的 policy_id/version 和实际审批人角色，供事后审计追溯——这一层不在这个 Skill 的即时返回值里，而是运行时侧带出的（见 main.py _resolve_pending_approval）。 |

## 版本演进示例

以下 Skill 存在草案版本，用于展示这套体系如何支持迭代/收紧权限，尚未接入运行时（不影响任何现有 Eval Case 的行为基线）：

- `send_confirmation_email` v2-draft（取代 `send_confirmation_email`）：[v2 草案，未接入运行时] 向客户发送本次拜访的确认邮件/通知。相较 v1，把'谁能真正触发外部发送'收紧为仅 travel_admin，employee/manager 调用时会被降级为生成待审草稿而非直接发出。

## 输入/输出 Schema 详情

以下按 Skill 逐个列出完整的输入 (`parameters`) 与成功态输出 (`returns`) JSON Schema，供接口对齐/契约测试直接引用：

### `get_customer_info` (v1)

输入 Schema：
```json
{
  "type": "object",
  "properties": {
    "company": {
      "type": "string",
      "description": "客户公司名称，如果还不知道可以留空"
    }
  }
}
```

输出 Schema（成功态）：
```json
{
  "type": "object",
  "properties": {
    "status": {
      "type": "string",
      "enum": [
        "success"
      ]
    },
    "customer_id": {
      "type": "string",
      "description": "稳定客户实体 ID，用于跨任务 scope 隔离"
    },
    "company_id": {
      "type": "string",
      "description": "稳定公司实体 ID"
    },
    "company": {
      "type": "string"
    },
    "address": {
      "type": "string"
    },
    "contact": {
      "type": "object",
      "properties": {
        "name": {
          "type": "string"
        },
        "title": {
          "type": "string"
        },
        "phone": {
          "type": "string"
        }
      }
    },
    "note": {
      "type": "string"
    }
  },
  "required": [
    "status",
    "customer_id",
    "company_id",
    "company",
    "address",
    "contact"
  ]
}
```

### `get_my_calendar` (v2)

输入 Schema：
```json
{
  "type": "object",
  "properties": {
    "date": {
      "type": "string",
      "description": "要查询的日期，例如 '下周二'"
    }
  },
  "required": [
    "date"
  ]
}
```

输出 Schema（成功态）：
```json
{
  "type": "object",
  "properties": {
    "status": {
      "type": "string",
      "enum": [
        "success"
      ]
    },
    "date": {
      "type": "string"
    },
    "events": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "time": {
            "type": "string"
          },
          "title": {
            "type": "string"
          }
        }
      }
    },
    "free_slots": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "source": {
      "type": "string",
      "enum": [
        "mock",
        "mcp"
      ]
    },
    "trace_id": {
      "type": "string"
    },
    "data_as_of": {
      "type": [
        "string",
        "null"
      ]
    },
    "latency_ms": {
      "type": "number"
    }
  },
  "required": [
    "status",
    "date",
    "events",
    "free_slots"
  ]
}
```

### `get_customer_calendar` (v1)

输入 Schema：
```json
{
  "type": "object",
  "properties": {
    "date": {
      "type": "string",
      "description": "要查询的日期，例如 '下周二'"
    }
  },
  "required": [
    "date"
  ]
}
```

输出 Schema（成功态）：
```json
{
  "type": "object",
  "properties": {
    "status": {
      "type": "string",
      "enum": [
        "success"
      ]
    },
    "date": {
      "type": "string"
    },
    "events": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "time": {
            "type": "string"
          },
          "title": {
            "type": "string"
          }
        }
      }
    },
    "free_slots": {
      "type": "array",
      "items": {
        "type": "string"
      }
    }
  },
  "required": [
    "status",
    "date",
    "events",
    "free_slots"
  ]
}
```

### `get_travel_policy` (v1)

输入 Schema：
```json
{
  "type": "object",
  "properties": {}
}
```

输出 Schema（成功态）：
```json
{
  "type": "object",
  "properties": {
    "status": {
      "type": "string",
      "enum": [
        "success"
      ]
    },
    "flight_budget": {
      "type": "number"
    },
    "hotel_budget_per_night": {
      "type": "number"
    },
    "notes": {
      "type": "string"
    },
    "policy_refs": {
      "type": "object",
      "description": "每条阈值对应的可引用政策原文（policy_id/version/effective_date/rule_text），供模型和最终报告引用出处，而不是只给裸数字。",
      "properties": {
        "flight_budget": {
          "type": [
            "object",
            "null"
          ]
        },
        "hotel_budget": {
          "type": [
            "object",
            "null"
          ]
        }
      }
    }
  },
  "required": [
    "status",
    "flight_budget",
    "hotel_budget_per_night"
  ]
}
```

### `search_flight` (v1)

输入 Schema：
```json
{
  "type": "object",
  "properties": {
    "date": {
      "type": "string",
      "description": "出发日期，例如 '下周二'"
    },
    "destination": {
      "type": "string",
      "description": "到达城市，例如 '上海'"
    }
  },
  "required": [
    "date"
  ]
}
```

输出 Schema（成功态）：
```json
{
  "type": "object",
  "properties": {
    "status": {
      "type": "string",
      "enum": [
        "success"
      ]
    },
    "flights": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "flight_id": {
            "type": "string"
          },
          "departure_time": {
            "type": "string"
          },
          "arrival_time": {
            "type": "string"
          },
          "price": {
            "type": "number"
          },
          "booking_status": {
            "type": "string",
            "enum": [
              "available",
              "sold_out"
            ]
          }
        },
        "required": [
          "flight_id",
          "departure_time",
          "arrival_time",
          "price",
          "booking_status"
        ]
      },
      "description": "查询时刻的候选快照；booking_status 不保证在 book_flight 提交时仍然成立（可能存在实时库存变化），这是设计内的行为，不是 bug。"
    }
  },
  "required": [
    "status",
    "flights"
  ]
}
```

### `search_hotel` (v1)

输入 Schema：
```json
{
  "type": "object",
  "properties": {
    "date": {
      "type": "string",
      "description": "入住日期，例如 '下周二'"
    },
    "near": {
      "type": "string",
      "description": "希望靠近的地点，例如客户公司地址或所在商圈"
    }
  },
  "required": [
    "date",
    "near"
  ]
}
```

输出 Schema（成功态）：
```json
{
  "type": "object",
  "properties": {
    "status": {
      "type": "string",
      "enum": [
        "success"
      ]
    },
    "options": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "hotel_id": {
            "type": "string"
          },
          "hotel_name": {
            "type": "string"
          },
          "price_per_night": {
            "type": "number"
          },
          "within_policy": {
            "type": "boolean"
          }
        },
        "required": [
          "hotel_id",
          "hotel_name",
          "price_per_night",
          "within_policy"
        ]
      }
    }
  },
  "required": [
    "status",
    "options"
  ]
}
```

### `delegate_booking_plan` (v1)

输入 Schema：
```json
{
  "type": "object",
  "properties": {
    "booking_type": {
      "type": "string",
      "enum": [
        "flight",
        "hotel"
      ]
    },
    "objective": {
      "type": "string",
      "description": "本次比较要达成的业务目标，不要复制整段对话历史。"
    },
    "constraints": {
      "type": "string",
      "description": "已确认的时间、预算或偏好约束；不能写入新的授权。"
    }
  },
  "required": [
    "booking_type",
    "objective"
  ]
}
```

输出 Schema（成功态）：
```json
{
  "type": "object",
  "properties": {
    "status": {
      "type": "string",
      "enum": [
        "success"
      ]
    },
    "proposal_id": {
      "type": "string"
    },
    "delegation_id": {
      "type": "string"
    },
    "booking_type": {
      "type": "string",
      "enum": [
        "flight",
        "hotel"
      ]
    },
    "recommended_option_id": {
      "type": "string"
    },
    "rationale": {
      "type": "string"
    },
    "alternatives_considered": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "risk_flags": {
      "type": "array",
      "items": {
        "type": "string"
      }
    }
  },
  "required": [
    "status",
    "proposal_id",
    "delegation_id",
    "booking_type",
    "recommended_option_id",
    "rationale"
  ]
}
```

### `delegate_policy_review` (v1)

输入 Schema：
```json
{
  "type": "object",
  "properties": {
    "proposal_id": {
      "type": "string",
      "description": "delegate_booking_plan 返回的 proposal_id。"
    }
  },
  "required": [
    "proposal_id"
  ]
}
```

输出 Schema（成功态）：
```json
{
  "type": "object",
  "properties": {
    "status": {
      "type": "string",
      "enum": [
        "success"
      ]
    },
    "review_id": {
      "type": "string"
    },
    "proposal_id": {
      "type": "string"
    },
    "model_decision": {
      "type": "string",
      "enum": [
        "ALLOW",
        "REQUIRE_APPROVAL",
        "DENY"
      ]
    },
    "runtime_decision": {
      "type": "string",
      "enum": [
        "ALLOW",
        "REQUIRE_APPROVAL",
        "DENY"
      ]
    },
    "effective_decision": {
      "type": "string",
      "enum": [
        "ALLOW",
        "REQUIRE_APPROVAL",
        "DENY"
      ]
    },
    "conflict": {
      "type": "boolean"
    },
    "policy_ids": {
      "type": "array",
      "items": {
        "type": "string"
      }
    }
  },
  "required": [
    "status",
    "review_id",
    "proposal_id",
    "model_decision",
    "runtime_decision",
    "effective_decision",
    "conflict"
  ]
}
```

### `book_flight` (v2)

输入 Schema：
```json
{
  "type": "object",
  "properties": {
    "flight_id": {
      "type": "string",
      "description": "要预订的航班编号，例如 'MU5101'"
    },
    "proposal_id": {
      "type": "string",
      "description": "delegate_booking_plan 返回且已完成 policy review 的 proposal_id。"
    }
  },
  "required": [
    "flight_id",
    "proposal_id"
  ]
}
```

输出 Schema（成功态）：
```json
{
  "type": "object",
  "description": "成功态（status=success）字段如下；失败态另有 status in {not_found, failed} 及 reason 字段，两者字段不同，Agent 需先判 status 再取字段。",
  "properties": {
    "status": {
      "type": "string",
      "enum": [
        "success"
      ]
    },
    "flight_id": {
      "type": "string"
    },
    "departure_time": {
      "type": "string"
    },
    "arrival_time": {
      "type": "string"
    },
    "price": {
      "type": "number"
    },
    "order_id": {
      "type": "string"
    },
    "message": {
      "type": "string"
    },
    "duplicate_call": {
      "type": "boolean",
      "description": "true 表示该 flight_id 此前已成功预订过，本次是幂等重放，未产生新订单。"
    }
  },
  "required": [
    "status",
    "flight_id",
    "order_id"
  ]
}
```

### `book_hotel` (v2)

输入 Schema：
```json
{
  "type": "object",
  "properties": {
    "hotel_id": {
      "type": "string",
      "description": "要预订的酒店编号，例如 'HTL-C'"
    },
    "proposal_id": {
      "type": "string",
      "description": "delegate_booking_plan 返回且已完成 policy review 的 proposal_id。"
    }
  },
  "required": [
    "hotel_id",
    "proposal_id"
  ]
}
```

输出 Schema（成功态）：
```json
{
  "type": "object",
  "description": "成功态（status=success）字段如下；失败态另有 status in {not_found, rejected} —— rejected 时带 policy_id + reason，说明依据哪条政策拦截。",
  "properties": {
    "status": {
      "type": "string",
      "enum": [
        "success"
      ]
    },
    "hotel_id": {
      "type": "string"
    },
    "hotel_name": {
      "type": "string"
    },
    "price_per_night": {
      "type": "number"
    },
    "confirmation_id": {
      "type": "string"
    },
    "message": {
      "type": "string"
    },
    "duplicate_call": {
      "type": "boolean",
      "description": "true 表示该 hotel_id 此前已成功预订过，本次是幂等重放，未产生新订单。"
    }
  },
  "required": [
    "status",
    "hotel_id",
    "confirmation_id"
  ]
}
```

### `sync_calendar` (v1)

输入 Schema：
```json
{
  "type": "object",
  "properties": {
    "event_title": {
      "type": "string",
      "description": "日历事件标题，例如 '上海客户拜访'"
    },
    "date": {
      "type": "string",
      "description": "日期"
    },
    "time": {
      "type": "string",
      "description": "会面时间"
    }
  },
  "required": [
    "event_title",
    "date",
    "time"
  ]
}
```

输出 Schema（成功态）：
```json
{
  "type": "object",
  "properties": {
    "status": {
      "type": "string",
      "enum": [
        "success"
      ]
    },
    "event_id": {
      "type": "string"
    }
  },
  "required": [
    "status",
    "event_id"
  ]
}
```

### `send_confirmation_email` (v1)

输入 Schema：
```json
{
  "type": "object",
  "properties": {
    "to": {
      "type": "string",
      "description": "收件人，例如客户联系人姓名或邮箱"
    },
    "summary": {
      "type": "string",
      "description": "本次拜访行程的简要总结，会写进邮件正文"
    }
  },
  "required": [
    "to",
    "summary"
  ]
}
```

输出 Schema（成功态）：
```json
{
  "type": "object",
  "properties": {
    "status": {
      "type": "string",
      "enum": [
        "success"
      ]
    },
    "message_id": {
      "type": "string"
    }
  },
  "required": [
    "status",
    "message_id"
  ]
}
```

### `request_human_approval` (v1)

输入 Schema：
```json
{
  "type": "object",
  "properties": {
    "reason": {
      "type": "string",
      "description": "为什么需要人工审批，用自然语言说明当前情况"
    },
    "proposed_action": {
      "type": "string",
      "description": "如果获得批准，你打算执行的具体动作"
    },
    "proposal_id": {
      "type": "string",
      "description": "酒店超预算审批必须传入对应 proposal_id；邮件发送审批可省略。"
    }
  },
  "required": [
    "reason",
    "proposed_action"
  ]
}
```

输出 Schema（成功态）：
```json
{
  "type": "object",
  "description": "此工具本身不会立即返回结果给模型——运行时会暂停并持久化会话，等待人工决策后，才把下面这个结构作为 tool 消息回填。status=approved 时后续可继续原计划；status=rejected 时 message 会附带拒绝说明，Agent 应据此重新规划。这个即时返回值本身不带 policy_id——政策引用是运行时额外记录进 approval_records 的审计信息，不塞进反馈给模型的这条 tool 消息里，避免和模型的下一步决策耦合。",
  "properties": {
    "status": {
      "type": "string",
      "enum": [
        "approved",
        "rejected"
      ]
    },
    "message": {
      "type": "string"
    }
  },
  "required": [
    "status",
    "message"
  ]
}
```
