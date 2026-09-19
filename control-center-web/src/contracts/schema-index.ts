/* eslint-disable */
/**
 * This file is generated. Do not edit it by hand.
 * The values are byte-stable projections of rag_ime/contracts/json/*.json.
 */

export const contractSchemas = {
  "active-rag-start.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.active-rag-start.v1",
    "type": "object",
    "properties": {
      "selectedText": {
        "type": "string"
      },
      "selected_text": {
        "type": "string"
      },
      "selectedTextHash": {
        "type": "string"
      },
      "selected_text_hash": {
        "type": "string"
      },
      "privacyDisposition": {
        "type": "string",
        "enum": [
          "allowed",
          "sensitive",
          "unknown"
        ]
      },
      "sensitiveField": {
        "type": "boolean"
      },
      "secureInput": {
        "type": "boolean"
      },
      "isPasswordField": {
        "type": "boolean"
      },
      "credentialField": {
        "type": "boolean"
      },
      "app": {
        "type": "string"
      },
      "frontAppBundleId": {
        "type": "string"
      },
      "windowContext": {
        "type": "object",
        "required": [
          "schemaVersion",
          "captureMode",
          "snapshotId",
          "revision",
          "application",
          "nodes"
        ],
        "properties": {
          "schemaVersion": {
            "const": "rag-ime.window-context.v1"
          },
          "captureMode": {
            "type": "string",
            "enum": [
              "accessibility_semantics",
              "terminal_visible_range"
            ]
          },
          "snapshotId": {
            "type": "string",
            "maxLength": 200
          },
          "revision": {
            "type": "integer",
            "minimum": 1
          },
          "capturedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "privacyDisposition": {
            "type": "string",
            "enum": [
              "allowed",
              "sensitive",
              "unknown"
            ]
          },
          "application": {
            "type": "object",
            "properties": {
              "pid": {
                "type": "integer",
                "minimum": 0
              },
              "bundleId": {
                "type": "string",
                "maxLength": 300
              },
              "name": {
                "type": "string",
                "maxLength": 160
              },
              "windowTitle": {
                "type": "string",
                "maxLength": 240
              }
            },
            "additionalProperties": false
          },
          "focusedNodeRef": {
            "type": "string",
            "maxLength": 200
          },
          "nodes": {
            "type": "array",
            "maxItems": 160
          },
          "nodeCount": {
            "type": "integer",
            "minimum": 0,
            "maximum": 160
          },
          "truncated": {
            "type": "boolean"
          },
          "semanticText": {
            "type": "string",
            "maxLength": 12000
          }
        },
        "additionalProperties": false
      }
    },
    "additionalProperties": true
  },
  "active-rag-status.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.active-rag-status.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "sessionId",
      "status",
      "evidenceCount",
      "candidateCount",
      "diagnostics"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.active-rag-service.v1"
      },
      "sessionId": {
        "type": "string",
        "minLength": 1
      },
      "status": {
        "type": "string"
      },
      "evidenceCount": {
        "type": "integer",
        "minimum": 0
      },
      "candidateCount": {
        "type": "integer",
        "minimum": 0
      },
      "expiresAfterMs": {
        "type": "integer",
        "minimum": 0
      },
      "candidates": {
        "type": "array"
      },
      "evidence": {
        "type": "array",
        "maxItems": 8,
        "items": {
          "$ref": "#/$defs/redactedEvidence"
        }
      },
      "stored": {
        "type": "boolean"
      },
      "noStore": {
        "type": "boolean"
      },
      "privacyAssessment": {
        "type": "object"
      },
      "storageReceipt": {
        "type": "object"
      },
      "diagnostics": {
        "type": "object",
        "required": [
          "contextInjection",
          "retrieval",
          "remoteModel"
        ],
        "properties": {
          "contextInjection": {
            "type": "object",
            "required": [
              "applied",
              "source",
              "contextChars",
              "contextHash",
              "selectedTextChars",
              "warnings"
            ],
            "properties": {
              "applied": {
                "type": "boolean"
              },
              "source": {
                "type": "string"
              },
              "contextChars": {
                "type": "integer",
                "minimum": 0
              },
              "contextHash": {
                "type": "string"
              },
              "selectedTextChars": {
                "type": "integer",
                "minimum": 0
              },
              "warnings": {
                "type": "array",
                "items": {
                  "type": "string"
                }
              }
            }
          },
          "retrieval": {
            "type": "object",
            "required": [
              "called",
              "evidenceCount",
              "lanes",
              "elapsedMs"
            ],
            "properties": {
              "called": {
                "type": "boolean"
              },
              "evidenceCount": {
                "type": "integer",
                "minimum": 0
              },
              "lanes": {
                "type": "object"
              },
              "elapsedMs": {
                "type": "number",
                "minimum": 0
              }
            }
          },
          "remoteModel": {
            "type": "object",
            "required": [
              "requested",
              "allowed",
              "provider",
              "model",
              "skipReason",
              "elapsedMs"
            ],
            "properties": {
              "requested": {
                "type": "boolean"
              },
              "allowed": {
                "type": "boolean"
              },
              "provider": {
                "type": "string"
              },
              "model": {
                "type": "string"
              },
              "skipReason": {
                "type": "string"
              },
              "elapsedMs": {
                "type": "number",
                "minimum": 0
              }
            }
          },
          "contextView": {
            "$ref": "#/$defs/contextView"
          },
          "progress": {
            "type": "object",
            "required": [
              "stage",
              "elapsedMs",
              "context",
              "retrieval",
              "model"
            ],
            "properties": {
              "stage": {
                "type": "string"
              },
              "elapsedMs": {
                "type": "integer",
                "minimum": 0
              },
              "context": {
                "type": "object",
                "properties": {
                  "foregroundChars": {
                    "type": "integer",
                    "minimum": 0
                  },
                  "windowNodeCount": {
                    "type": "integer",
                    "minimum": 0
                  },
                  "windowCaptureMode": {
                    "type": "string"
                  },
                  "recentInputCount": {
                    "type": "integer",
                    "minimum": 0
                  },
                  "recentInputChars": {
                    "type": "integer",
                    "minimum": 0
                  },
                  "recentInputUsed": {
                    "type": "boolean"
                  }
                }
              },
              "retrieval": {
                "type": "object",
                "properties": {
                  "attempted": {
                    "type": "boolean"
                  },
                  "elapsedMs": {
                    "type": "number",
                    "minimum": 0
                  },
                  "retrievedCount": {
                    "type": "integer",
                    "minimum": 0
                  },
                  "evidenceCount": {
                    "type": "integer",
                    "minimum": 0
                  },
                  "contextEvidenceCount": {
                    "type": "integer",
                    "minimum": 0
                  },
                  "items": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {
                      "type": "object",
                      "properties": {
                        "sourceType": {
                          "type": "string"
                        },
                        "sourceLane": {
                          "type": "string"
                        },
                        "title": {
                          "type": "string"
                        },
                        "preview": {
                          "type": "string"
                        }
                      }
                    }
                  }
                }
              },
              "model": {
                "type": "object",
                "properties": {
                  "attempted": {
                    "type": "boolean"
                  },
                  "partialVisible": {
                    "type": "boolean"
                  },
                  "partialChars": {
                    "type": "integer",
                    "minimum": 0
                  },
                  "firstTokenMs": {
                    "type": "integer",
                    "minimum": 0
                  },
                  "providerFirstTokenMs": {
                    "type": "integer",
                    "minimum": 0
                  },
                  "providerElapsedMs": {
                    "type": "integer",
                    "minimum": 0
                  },
                  "qualityRetry": {
                    "type": "boolean"
                  },
                  "qualityRetryReason": {
                    "type": "string"
                  }
                }
              }
            }
          }
        }
      },
      "traceEvents": {
        "type": "array"
      }
    },
    "$defs": {
      "redactedEvidence": {
        "type": "object",
        "required": [
          "evidenceId",
          "sourceType",
          "sourceLane",
          "score",
          "confidence",
          "tags",
          "hasPreview"
        ],
        "properties": {
          "evidenceId": {
            "type": "string"
          },
          "sourceType": {
            "type": "string"
          },
          "sourceLane": {
            "type": "string"
          },
          "score": {
            "type": "number"
          },
          "confidence": {
            "type": "number"
          },
          "tags": {
            "type": "array",
            "maxItems": 6,
            "items": {
              "type": "string"
            }
          },
          "hasPreview": {
            "type": "boolean"
          }
        },
        "additionalProperties": false
      },
      "contextView": {
        "type": "object",
        "required": [
          "schemaVersion",
          "source",
          "currentRequest",
          "currentContext",
          "selectedText",
          "taskMode",
          "groundingMode",
          "windowContext",
          "recentCompleteInputs",
          "planning",
          "activityTimeline",
          "groundingEvidence",
          "evidenceHints",
          "contextBudget"
        ],
        "properties": {
          "schemaVersion": {
            "const": "rag-ime.active-rag-context-view.v1"
          },
          "source": {
            "type": "string",
            "enum": [
              "frontend_request",
              "provider_request"
            ]
          },
          "currentRequest": {
            "type": "string"
          },
          "currentContext": {
            "type": "string"
          },
          "selectedText": {
            "type": "string"
          },
          "taskMode": {
            "type": "string"
          },
          "groundingMode": {
            "type": "string"
          },
          "windowContext": {
            "type": "object"
          },
          "recentCompleteInputs": {
            "type": "array",
            "maxItems": 4
          },
          "planning": {
            "type": "object"
          },
          "activityTimeline": {
            "type": "object"
          },
          "groundingEvidence": {
            "type": "array",
            "maxItems": 12
          },
          "evidenceHints": {
            "type": "array",
            "maxItems": 12
          },
          "contextBudget": {
            "type": "object"
          }
        },
        "additionalProperties": false
      }
    }
  },
  "activity-timeline-context.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.activity-timeline-context.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "available",
      "date",
      "timelineId",
      "status",
      "sourceEventHash",
      "summary",
      "segments",
      "eventCount",
      "retainedEventCount",
      "filteredInternalEventCount",
      "deduplicatedEventCount",
      "redactedEventCount",
      "corroborationOnly",
      "maySupportFacts"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.activity-timeline-context.v1"
      },
      "available": {
        "type": "boolean"
      },
      "date": {
        "type": "string",
        "pattern": "^\\d{4}-\\d{2}-\\d{2}$"
      },
      "timelineId": {
        "type": "string"
      },
      "status": {
        "type": "string",
        "enum": [
          "unavailable",
          "draft",
          "approved"
        ]
      },
      "sourceEventHash": {
        "type": "string",
        "pattern": "^$|^[a-f0-9]{64}$"
      },
      "summary": {
        "type": "string",
        "maxLength": 2400
      },
      "segments": {
        "type": "array",
        "maxItems": 12,
        "items": {
          "$ref": "#/$defs/segment"
        }
      },
      "eventCount": {
        "type": "integer",
        "minimum": 0
      },
      "retainedEventCount": {
        "type": "integer",
        "minimum": 0
      },
      "filteredInternalEventCount": {
        "type": "integer",
        "minimum": 0
      },
      "deduplicatedEventCount": {
        "type": "integer",
        "minimum": 0
      },
      "redactedEventCount": {
        "type": "integer",
        "minimum": 0
      },
      "corroborationOnly": {
        "type": "boolean",
        "const": true
      },
      "maySupportFacts": {
        "type": "boolean",
        "const": false
      },
      "source": {
        "$ref": "#/$defs/source"
      },
      "ref": {
        "$ref": "#/$defs/ref"
      }
    },
    "$defs": {
      "segment": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "segmentId",
          "position",
          "app",
          "sourceKinds",
          "contextGroupIds",
          "startMs",
          "endMs",
          "eventCount",
          "summary",
          "redactedEventCount"
        ],
        "properties": {
          "segmentId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "position": {
            "type": "integer",
            "minimum": 0
          },
          "app": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "sourceKinds": {
            "type": "array",
            "maxItems": 12,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 120
            },
            "uniqueItems": true
          },
          "contextGroupIds": {
            "type": "array",
            "maxItems": 12,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 240
            },
            "uniqueItems": true
          },
          "startMs": {
            "type": "integer",
            "minimum": 0
          },
          "endMs": {
            "type": "integer",
            "minimum": 0
          },
          "eventCount": {
            "type": "integer",
            "minimum": 1
          },
          "summary": {
            "type": "string",
            "minLength": 1,
            "maxLength": 760
          },
          "redactedEventCount": {
            "type": "integer",
            "minimum": 0
          },
          "title": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "apps": {
            "type": "array",
            "maxItems": 12,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 240
            },
            "uniqueItems": true
          },
          "source": {
            "$ref": "#/$defs/source"
          },
          "ref": {
            "$ref": "#/$defs/ref"
          }
        }
      },
      "source": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "type",
          "id"
        ],
        "properties": {
          "type": {
            "type": "string",
            "const": "activity_timeline"
          },
          "id": {
            "type": "string",
            "minLength": 1
          }
        }
      },
      "ref": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "type",
          "id"
        ],
        "properties": {
          "type": {
            "type": "string",
            "const": "timeline"
          },
          "id": {
            "type": "string",
            "minLength": 1
          },
          "segmentId": {
            "type": "string",
            "minLength": 1
          }
        }
      }
    }
  },
  "agent-approval-model-decision.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://wisdom-weasel.local/contracts/agent-approval-model-decision.v1.json",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "receiptId",
      "approvalId",
      "sessionId",
      "mode",
      "contextKind",
      "contextId",
      "historyEntryCount",
      "automatic",
      "decision",
      "status",
      "modelProvider",
      "modelId",
      "modelProfile",
      "thinkingLevel",
      "promptVersion",
      "payloadSha256",
      "inputSha256",
      "scopeSha256",
      "reasonCodes",
      "rationaleSummary",
      "createdAtMs",
      "decidedAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.agent-approval-model-decision.v1"
      },
      "receiptId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 200
      },
      "approvalId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 200
      },
      "sessionId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 200
      },
      "contextKind": {
        "type": "string",
        "enum": [
          "session",
          "room"
        ]
      },
      "contextId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 200
      },
      "historyEntryCount": {
        "type": "integer",
        "minimum": 0,
        "maximum": 32
      },
      "mode": {
        "const": "model"
      },
      "automatic": {
        "const": true
      },
      "decision": {
        "type": "string",
        "enum": [
          "approve",
          "deny"
        ]
      },
      "status": {
        "type": "string",
        "enum": [
          "decided",
          "failed_closed"
        ]
      },
      "modelProvider": {
        "type": "string",
        "enum": [
          "openai-codex",
          "typesafe"
        ]
      },
      "modelId": {
        "type": "string",
        "enum": [
          "gpt-5.6-luna",
          "jev-latest",
          "jev-preview",
          "jev-1.13.0"
        ]
      },
      "modelProfile": {
        "type": "string",
        "enum": [
          "openai-codex/gpt-5.6-luna",
          "typesafe/jev-latest",
          "typesafe/jev-preview",
          "typesafe/jev-1.13.0"
        ]
      },
      "thinkingLevel": {
        "type": "string",
        "enum": [
          "max",
          "structured"
        ]
      },
      "promptVersion": {
        "type": "string",
        "enum": [
          "approval-arbiter-v1",
          "approval-arbiter-v2",
          "approval-arbiter-jev-v1"
        ]
      },
      "payloadSha256": {
        "type": "string",
        "pattern": "^[a-f0-9]{64}$"
      },
      "inputSha256": {
        "type": "string",
        "pattern": "^[a-f0-9]{64}$"
      },
      "scopeSha256": {
        "type": "string",
        "pattern": "^(?:[a-f0-9]{64})?$"
      },
      "reasonCodes": {
        "type": "array",
        "minItems": 1,
        "maxItems": 8,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "enum": [
            "bounded_operation",
            "authorized_scope",
            "requested_effect_matches_preview",
            "destructive_effect",
            "sensitive_target",
            "network_effect",
            "irreversible_effect",
            "cross_workspace",
            "insufficient_evidence",
            "scope_not_authorized",
            "prompt_injection_detected",
            "policy_boundary",
            "model_unavailable",
            "model_timeout",
            "model_invalid_response",
            "approval_stale"
          ]
        }
      },
      "rationaleSummary": {
        "type": "string",
        "minLength": 1,
        "maxLength": 500
      },
      "failureCode": {
        "type": "string",
        "maxLength": 80
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "decidedAtMs": {
        "type": "integer",
        "minimum": 0
      }
    }
  },
  "agent-approval.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-approval.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "approvalId",
      "sessionId",
      "toolId",
      "operation",
      "payloadSha256",
      "preview",
      "riskLevel",
      "state",
      "requestedAtMs",
      "expiresAtMs",
      "decidedBy",
      "causalMetadata"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-approval.v1"
      },
      "approvalId": {
        "type": "string",
        "minLength": 1
      },
      "sessionId": {
        "type": "string",
        "minLength": 1
      },
      "toolCallId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 512
      },
      "toolId": {
        "type": "string",
        "minLength": 1
      },
      "operation": {
        "type": "string",
        "minLength": 1
      },
      "payloadSha256": {
        "type": "string",
        "minLength": 64
      },
      "preview": {
        "type": "object"
      },
      "riskLevel": {
        "type": "string",
        "enum": [
          "R1",
          "R2",
          "R3"
        ]
      },
      "state": {
        "type": "string",
        "enum": [
          "pending",
          "approved",
          "external_pending",
          "rejected",
          "expired",
          "stale",
          "applied",
          "failed"
        ]
      },
      "requestedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "expiresAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "decidedBy": {
        "type": "string",
        "maxLength": 120
      },
      "decidedAtMs": {
        "type": [
          "integer",
          "null"
        ]
      },
      "receipt": {
        "type": [
          "object",
          "null"
        ]
      },
      "causalMetadata": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "todoId",
          "todoRevision",
          "goalId",
          "goalRevision",
          "turnId",
          "roomBound",
          "roomId",
          "rootId",
          "dispatchId",
          "generation"
        ],
        "properties": {
          "todoId": {
            "type": "string",
            "maxLength": 240
          },
          "todoRevision": {
            "type": "integer",
            "minimum": 0
          },
          "goalId": {
            "type": "string",
            "maxLength": 240
          },
          "goalRevision": {
            "type": "integer",
            "minimum": 0
          },
          "turnId": {
            "type": "string",
            "maxLength": 240
          },
          "roomBound": {
            "type": "boolean"
          },
          "roomId": {
            "type": "string",
            "maxLength": 240
          },
          "rootId": {
            "type": "string",
            "maxLength": 240
          },
          "dispatchId": {
            "type": "string",
            "maxLength": 240
          },
          "generation": {
            "type": "integer",
            "minimum": 0
          }
        }
      }
    }
  },
  "agent-artifact-inspection.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-artifact-inspection.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "artifact",
      "records",
      "totalRecords",
      "returnedRecords",
      "truncated",
      "limits"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-artifact-inspection.v1"
      },
      "artifact": {
        "type": "object"
      },
      "records": {
        "type": "array",
        "maxItems": 500,
        "items": {
          "type": "object"
        }
      },
      "totalRecords": {
        "type": "integer",
        "minimum": 0
      },
      "returnedRecords": {
        "type": "integer",
        "minimum": 0,
        "maximum": 500
      },
      "truncated": {
        "type": "boolean"
      },
      "limits": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "requestedRecords",
          "maxRecords",
          "maxOutputBytes"
        ],
        "properties": {
          "requestedRecords": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500
          },
          "maxRecords": {
            "type": "integer",
            "const": 500
          },
          "maxOutputBytes": {
            "type": "integer",
            "const": 262144
          }
        }
      }
    }
  },
  "agent-artifact-ref.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-artifact-ref.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "artifactId",
      "ownerKind",
      "ownerId",
      "kind",
      "mediaType",
      "appendOnly",
      "byteSize",
      "sha256",
      "recordCount",
      "snapshotRevision",
      "snapshotSha256",
      "createdAtMs",
      "updatedAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-artifact-ref.v1"
      },
      "artifactId": {
        "type": "string",
        "minLength": 1
      },
      "ownerKind": {
        "type": "string",
        "enum": [
          "subagent_run",
          "tool_run",
          "connector_run"
        ]
      },
      "ownerId": {
        "type": "string",
        "minLength": 1
      },
      "kind": {
        "type": "string",
        "minLength": 1
      },
      "mediaType": {
        "type": "string",
        "minLength": 1
      },
      "appendOnly": {
        "type": "boolean"
      },
      "byteSize": {
        "type": "integer",
        "minimum": 0
      },
      "sha256": {
        "type": "string",
        "pattern": "^$|^[a-f0-9]{64}$"
      },
      "recordCount": {
        "type": "integer",
        "minimum": 0
      },
      "snapshotRevision": {
        "type": "integer",
        "minimum": 0
      },
      "snapshotSha256": {
        "type": "string",
        "pattern": "^$|^[a-f0-9]{64}$"
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "updatedAtMs": {
        "type": "integer",
        "minimum": 0
      }
    }
  },
  "agent-background-job.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-background-job.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "jobId",
      "sessionId",
      "label",
      "status",
      "command",
      "commandSha256",
      "cwd",
      "networkAllowed",
      "maxRunSeconds",
      "pid",
      "createdAtMs",
      "startedAtMs",
      "updatedAtMs",
      "endedAtMs",
      "exitCode",
      "outputBytes",
      "logStartCursor",
      "logTruncated",
      "cancelRequestedAtMs",
      "error",
      "approvalId",
      "causalMetadata"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-background-job.v1"
      },
      "jobId": {
        "type": "string",
        "pattern": "^bg_[a-f0-9]{32}$"
      },
      "sessionId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "label": {
        "type": "string",
        "minLength": 1,
        "maxLength": 120
      },
      "status": {
        "type": "string",
        "enum": [
          "queued",
          "running",
          "cancelling",
          "completed",
          "failed",
          "cancelled",
          "orphaned"
        ]
      },
      "command": {
        "type": "string",
        "minLength": 1,
        "maxLength": 2000
      },
      "commandSha256": {
        "type": "string",
        "pattern": "^[a-f0-9]{64}$"
      },
      "cwd": {
        "type": "string",
        "minLength": 1,
        "maxLength": 4096
      },
      "networkAllowed": {
        "type": "boolean"
      },
      "maxRunSeconds": {
        "type": "integer",
        "minimum": 1,
        "maximum": 86400
      },
      "pid": {
        "type": [
          "integer",
          "null"
        ],
        "minimum": 1
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "startedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "updatedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "endedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "exitCode": {
        "type": [
          "integer",
          "null"
        ]
      },
      "outputBytes": {
        "type": "integer",
        "minimum": 0
      },
      "logStartCursor": {
        "type": "integer",
        "minimum": 0
      },
      "logTruncated": {
        "type": "boolean"
      },
      "cancelRequestedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "error": {
        "type": "string",
        "maxLength": 500
      },
      "approvalId": {
        "type": "string",
        "maxLength": 240
      },
      "causalMetadata": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "todoId",
          "todoRevision",
          "goalId",
          "goalRevision",
          "turnId",
          "roomBound"
        ],
        "properties": {
          "todoId": {
            "type": "string",
            "maxLength": 240
          },
          "todoRevision": {
            "type": "integer",
            "minimum": 0
          },
          "goalId": {
            "type": "string",
            "maxLength": 240
          },
          "goalRevision": {
            "type": "integer",
            "minimum": 0
          },
          "turnId": {
            "type": "string",
            "maxLength": 240
          },
          "roomBound": {
            "type": "boolean"
          }
        }
      },
      "roomLineage": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "roomId",
          "rootId",
          "generation",
          "taskId",
          "dispatchId"
        ],
        "properties": {
          "roomId": {
            "type": "string",
            "maxLength": 240
          },
          "rootId": {
            "type": "string",
            "maxLength": 240
          },
          "generation": {
            "type": "integer",
            "minimum": 0
          },
          "taskId": {
            "type": "string",
            "maxLength": 240
          },
          "dispatchId": {
            "type": "string",
            "maxLength": 240
          }
        }
      }
    },
    "additionalProperties": false
  },
  "agent-configuration.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-configuration.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "revision",
      "revisionToken",
      "configuration",
      "sync",
      "updatedAtMs",
      "updatedBy",
      "lastEventId"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-configuration.v1"
      },
      "revision": {
        "type": "integer",
        "minimum": 1
      },
      "revisionToken": {
        "type": "string",
        "minLength": 1
      },
      "configuration": {
        "type": "object",
        "required": [
          "runtime",
          "sessionDefaults",
          "coordination",
          "modelRouting",
          "skillRouting",
          "scenarioPolicies"
        ],
        "properties": {
          "runtime": {
            "type": "object",
            "required": [
              "enabled",
              "startup",
              "idleTimeoutSeconds"
            ],
            "properties": {
              "enabled": {
                "type": "boolean"
              },
              "startup": {
                "type": "string",
                "const": "lazy"
              },
              "idleTimeoutSeconds": {
                "type": "integer",
                "minimum": 0
              }
            }
          },
          "sessionDefaults": {
            "type": "object",
            "required": [
              "resumeLastSession",
              "roleId",
              "roleVersion",
              "modelProfile",
              "toolProfileVersion",
              "capabilityDisclosurePreferences"
            ],
            "properties": {
              "resumeLastSession": {
                "type": "boolean"
              },
              "roleId": {
                "type": "string",
                "minLength": 1
              },
              "roleVersion": {
                "type": "string",
                "minLength": 1
              },
              "modelProfile": {
                "type": "string",
                "minLength": 1
              },
              "toolProfileVersion": {
                "type": "string",
                "minLength": 1
              },
              "capabilityDisclosurePreferences": {
                "type": "object",
                "additionalProperties": {
                  "type": "string",
                  "enum": [
                    "inherit",
                    "enabled",
                    "disabled"
                  ]
                }
              }
            }
          },
          "coordination": {
            "type": "object",
            "required": [
              "enabled"
            ],
            "properties": {
              "enabled": {
                "type": "boolean"
              }
            }
          },
          "modelRouting": {
            "type": "object",
            "required": [
              "primary",
              "traceDiagnostic",
              "toolAgent",
              "subagent",
              "roomCoordinator"
            ],
            "properties": {
              "primary": {
                "$ref": "#/$defs/modelRoute"
              },
              "traceDiagnostic": {
                "$ref": "#/$defs/modelRoute"
              },
              "toolAgent": {
                "$ref": "#/$defs/modelRoute"
              },
              "subagent": {
                "$ref": "#/$defs/modelRoute"
              },
              "roomCoordinator": {
                "$ref": "#/$defs/modelRoute"
              }
            },
            "additionalProperties": false
          },
          "prompts": {
            "type": "object",
            "required": [
              "systemInstructions",
              "compactionInstructions"
            ],
            "properties": {
              "systemInstructions": {
                "type": "string",
                "maxLength": 8000
              },
              "compactionInstructions": {
                "type": "string",
                "maxLength": 8000
              }
            },
            "additionalProperties": false
          },
          "skillRouting": {
            "type": "object",
            "required": [
              "ordinary",
              "room",
              "trace",
              "agentLab"
            ],
            "properties": {
              "ordinary": {
                "$ref": "#/$defs/skillRoute"
              },
              "room": {
                "$ref": "#/$defs/skillRoute"
              },
              "trace": {
                "$ref": "#/$defs/skillRoute"
              },
              "agentLab": {
                "$ref": "#/$defs/skillRoute"
              }
            },
            "additionalProperties": false
          },
          "scenarioPolicies": {
            "type": "object",
            "required": [
              "ordinary",
              "room",
              "trace",
              "agentLab"
            ],
            "properties": {
              "ordinary": {
                "$ref": "#/$defs/scenarioPolicy"
              },
              "room": {
                "$ref": "#/$defs/scenarioPolicy"
              },
              "trace": {
                "$ref": "#/$defs/scenarioPolicy"
              },
              "agentLab": {
                "$ref": "#/$defs/scenarioPolicy"
              }
            },
            "additionalProperties": false
          }
        }
      },
      "sync": {
        "type": "object",
        "required": [
          "state",
          "appliedRevision",
          "error"
        ],
        "properties": {
          "state": {
            "type": "string",
            "enum": [
              "synchronized",
              "pending",
              "failed"
            ]
          },
          "appliedRevision": {
            "type": "integer",
            "minimum": 0
          },
          "error": {
            "type": "string"
          }
        }
      },
      "updatedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "updatedBy": {
        "type": "string",
        "minLength": 1
      },
      "lastEventId": {
        "type": "string"
      }
    },
    "$defs": {
      "modelRoute": {
        "type": "object",
        "required": [
          "modelProfile",
          "thinkingLevel"
        ],
        "properties": {
          "modelProfile": {
            "type": "string",
            "minLength": 1
          },
          "thinkingLevel": {
            "type": "string",
            "enum": [
              "inherit",
              "off",
              "minimal",
              "low",
              "medium",
              "high",
              "xhigh",
              "max"
            ]
          }
        },
        "additionalProperties": false
      },
      "skillRoute": {
        "type": "array",
        "maxItems": 128,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "minLength": 1,
          "maxLength": 128,
          "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"
        }
      },
      "scenarioPolicy": {
        "type": "object",
        "required": [
          "promptInstructions",
          "toolAllowlist"
        ],
        "properties": {
          "promptInstructions": {
            "type": "string",
            "maxLength": 8000
          },
          "toolAllowlist": {
            "type": "array",
            "uniqueItems": true,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 128
            }
          }
        },
        "additionalProperties": false
      }
    }
  },
  "agent-context-item.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-context-item.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "itemId",
      "sessionId",
      "sourceKind",
      "sourceId",
      "lane",
      "lifecycle",
      "status",
      "title",
      "summary",
      "availableAtMs",
      "expiresAtMs",
      "deliveredTurnId",
      "createdAtMs",
      "updatedAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-context-item.v1"
      },
      "itemId": {
        "type": "string",
        "minLength": 1
      },
      "sessionId": {
        "type": "string",
        "minLength": 1
      },
      "sourceKind": {
        "type": "string",
        "minLength": 1
      },
      "sourceId": {
        "type": "string"
      },
      "lane": {
        "type": "string",
        "enum": [
          "result",
          "status",
          "notification",
          "room",
          "schedule",
          "fact"
        ]
      },
      "lifecycle": {
        "type": "string",
        "enum": [
          "once",
          "turn",
          "until_ack",
          "persistent"
        ]
      },
      "status": {
        "type": "string",
        "enum": [
          "pending",
          "delivered",
          "consumed",
          "acknowledged",
          "expired"
        ]
      },
      "title": {
        "type": "string",
        "minLength": 1
      },
      "summary": {
        "type": "string"
      },
      "availableAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "expiresAtMs": {
        "type": [
          "integer",
          "null"
        ],
        "minimum": 0
      },
      "deliveredTurnId": {
        "type": "string"
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "updatedAtMs": {
        "type": "integer",
        "minimum": 0
      }
    }
  },
  "agent-context-trace.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-context-trace.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "traceId",
      "sessionId",
      "turnId",
      "sourceKind",
      "status",
      "finalFingerprint",
      "nodes",
      "edges",
      "createdAtMs",
      "updatedAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-context-trace.v1"
      },
      "traceId": {
        "type": "string",
        "minLength": 1
      },
      "sessionId": {
        "type": "string",
        "minLength": 1
      },
      "turnId": {
        "type": "string"
      },
      "sourceKind": {
        "type": "string",
        "minLength": 1
      },
      "status": {
        "type": "string",
        "enum": [
          "building",
          "accepted",
          "failed"
        ]
      },
      "finalFingerprint": {
        "type": "string",
        "pattern": "^$|^sha256:[a-f0-9]{16}$"
      },
      "nodes": {
        "type": "array",
        "maxItems": 64,
        "items": {
          "$ref": "#/$defs/node"
        }
      },
      "edges": {
        "type": "array",
        "maxItems": 128,
        "items": {
          "$ref": "#/$defs/edge"
        }
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "updatedAtMs": {
        "type": "integer",
        "minimum": 0
      }
    },
    "$defs": {
      "node": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "nodeId",
          "ordinal",
          "stage",
          "label",
          "sourceKind",
          "disposition",
          "summary",
          "charCount",
          "tokenEstimate",
          "durationMs",
          "fingerprint",
          "reason",
          "metadata",
          "createdAtMs"
        ],
        "properties": {
          "nodeId": {
            "type": "string",
            "minLength": 1
          },
          "ordinal": {
            "type": "integer",
            "minimum": 1
          },
          "stage": {
            "type": "string",
            "minLength": 1
          },
          "label": {
            "type": "string",
            "minLength": 1
          },
          "sourceKind": {
            "type": "string",
            "minLength": 1
          },
          "disposition": {
            "type": "string",
            "enum": [
              "included",
              "omitted",
              "redacted",
              "failed"
            ]
          },
          "summary": {
            "type": "string"
          },
          "charCount": {
            "type": "integer",
            "minimum": 0
          },
          "tokenEstimate": {
            "type": "integer",
            "minimum": 0
          },
          "durationMs": {
            "type": "integer",
            "minimum": 0
          },
          "fingerprint": {
            "type": "string",
            "pattern": "^$|^sha256:[a-f0-9]{16}$"
          },
          "reason": {
            "type": "string"
          },
          "metadata": {
            "type": "object"
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "edge": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "source",
          "target"
        ],
        "properties": {
          "source": {
            "type": "string",
            "minLength": 1
          },
          "target": {
            "type": "string",
            "minLength": 1
          }
        }
      }
    }
  },
  "agent-control-bootstrap.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-control-bootstrap.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "apiVersion",
      "configuration",
      "runtime",
      "capabilities",
      "platform",
      "routes"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-control-bootstrap.v1"
      },
      "apiVersion": {
        "type": "string",
        "const": "control-api.v1"
      },
      "configuration": {
        "type": "object"
      },
      "runtime": {
        "type": "object"
      },
      "capabilities": {
        "type": "object",
        "required": [
          "schemaVersion",
          "items"
        ],
        "properties": {
          "schemaVersion": {
            "type": "string",
            "const": "rag-ime.control-capability-list.v1"
          },
          "items": {
            "type": "array"
          }
        }
      },
      "platform": {
        "type": "object"
      },
      "routes": {
        "type": "array",
        "items": {
          "type": "object",
          "required": [
            "pathId",
            "method",
            "remoteSafe",
            "subscription",
            "params",
            "query",
            "target"
          ],
          "properties": {
            "pathId": {
              "type": "string",
              "minLength": 1
            },
            "method": {
              "type": "string",
              "enum": [
                "GET",
                "POST",
                "PATCH",
                "DELETE"
              ]
            },
            "remoteSafe": {
              "type": "boolean"
            },
            "subscription": {
              "type": "boolean"
            },
            "params": {
              "type": "array",
              "items": {
                "type": "string"
              }
            },
            "query": {
              "type": "array",
              "items": {
                "type": "string"
              }
            },
            "remoteScopes": {
              "type": "array",
              "items": {
                "type": "string"
              }
            },
            "remoteQuery": {
              "type": "array",
              "items": {
                "type": "string"
              }
            },
            "target": {
              "type": "object",
              "required": [
                "8766",
                "8768"
              ],
              "properties": {
                "8766": {
                  "type": "string",
                  "minLength": 1
                },
                "8768": {
                  "type": [
                    "string",
                    "null"
                  ],
                  "minLength": 1
                }
              }
            }
          }
        }
      }
    }
  },
  "agent-control-event.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-control-event.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "eventId",
      "sequence",
      "eventType",
      "createdAtMs",
      "payload",
      "resumeToken"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-control-event.v1"
      },
      "eventId": {
        "type": "string",
        "minLength": 1
      },
      "sequence": {
        "type": "integer",
        "minimum": 0
      },
      "eventType": {
        "type": "string",
        "enum": [
          "configuration_changed",
          "configuration_applied",
          "configuration_failed",
          "snapshot_required"
        ]
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "payload": {
        "type": "object"
      },
      "resumeToken": {
        "type": "string"
      }
    }
  },
  "agent-conversation-context.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-conversation-context.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "available",
      "date",
      "messages",
      "messageCount",
      "deduplicatedMessageCount",
      "redactedMessageCount",
      "corroborationOnly",
      "maySupportFacts"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-conversation-context.v1"
      },
      "available": {
        "type": "boolean"
      },
      "date": {
        "type": "string",
        "pattern": "^\\d{4}-\\d{2}-\\d{2}$"
      },
      "messages": {
        "type": "array",
        "maxItems": 24,
        "items": {
          "type": "object",
          "additionalProperties": false,
          "required": [
            "role",
            "sourceKind",
            "text",
            "occurredAtMs"
          ],
          "properties": {
            "role": {
              "type": "string",
              "enum": [
                "user",
                "assistant"
              ]
            },
            "sourceKind": {
              "type": "string",
              "enum": [
                "user_message",
                "assistant_message",
                "room_event",
                "session_digest"
              ]
            },
            "text": {
              "type": "string",
              "minLength": 1,
              "maxLength": 600
            },
            "occurredAtMs": {
              "type": "integer",
              "minimum": 0
            }
          }
        }
      },
      "messageCount": {
        "type": "integer",
        "minimum": 0
      },
      "deduplicatedMessageCount": {
        "type": "integer",
        "minimum": 0
      },
      "redactedMessageCount": {
        "type": "integer",
        "minimum": 0
      },
      "corroborationOnly": {
        "type": "boolean",
        "const": true
      },
      "maySupportFacts": {
        "type": "boolean",
        "const": false
      }
    }
  },
  "agent-event.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-event.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "eventId",
      "sessionId",
      "turnId",
      "sequence",
      "createdAtMs",
      "eventType",
      "payload",
      "resumeToken"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-event.v1"
      },
      "eventId": {
        "type": "string",
        "minLength": 1
      },
      "sessionId": {
        "type": "string",
        "minLength": 1
      },
      "turnId": {
        "type": "string"
      },
      "sequence": {
        "type": "integer",
        "minimum": 1
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "eventType": {
        "type": "string",
        "enum": [
          "snapshot",
          "text_delta",
          "reasoning_summary",
          "status_changed",
          "session_configuration_changed",
          "session_command_invoked",
          "message_queue_updated",
          "workflow_changed",
          "lifecycle_cancellation_changed",
          "tool_started",
          "tool_progress",
          "tool_finished",
          "approval_required",
          "approval_resolved",
          "background_job_started",
          "background_job_progress",
          "background_job_completed",
          "background_job_failed",
          "background_job_cancelled",
          "memory_checkpointed",
          "memory_maintenance_updated",
          "user_input_required",
          "message_completed",
          "provider_request_completed",
          "provider_request_failed",
          "compaction_started",
          "compaction_completed",
          "turn_completed",
          "turn_failed",
          "snapshot_required",
          "heartbeat"
        ]
      },
      "payload": {
        "type": "object"
      },
      "resumeToken": {
        "type": "string",
        "minLength": 1
      }
    }
  },
  "agent-file-descriptor.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-file-descriptor.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "mediaId",
      "sessionId",
      "fileName",
      "mimeType",
      "byteSize",
      "sha256",
      "previewKind",
      "language",
      "contentUrl"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-file-descriptor.v1"
      },
      "mediaId": {
        "type": "string",
        "pattern": "^media_[A-Za-z0-9_-]{12,80}$"
      },
      "sessionId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "fileName": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "mimeType": {
        "type": "string",
        "minLength": 1,
        "maxLength": 120
      },
      "byteSize": {
        "type": "integer",
        "minimum": 1
      },
      "sha256": {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$"
      },
      "previewKind": {
        "type": "string",
        "enum": [
          "markdown",
          "code",
          "diff",
          "image",
          "html",
          "unsupported"
        ]
      },
      "language": {
        "type": "string",
        "maxLength": 40
      },
      "contentUrl": {
        "type": "string",
        "pattern": "^/api/agent/media/[^/]+/content\\?sessionId="
      }
    },
    "additionalProperties": false
  },
  "agent-file-preview.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-file-preview.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "descriptor",
      "content",
      "previewByteSize",
      "truncated"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-file-preview.v1"
      },
      "descriptor": {
        "type": "object",
        "required": [
          "schemaVersion",
          "mediaId",
          "sessionId",
          "fileName",
          "mimeType",
          "byteSize",
          "sha256",
          "previewKind",
          "language",
          "contentUrl"
        ],
        "properties": {
          "schemaVersion": {
            "type": "string",
            "const": "rag-ime.agent-file-descriptor.v1"
          },
          "mediaId": {
            "type": "string",
            "pattern": "^media_[A-Za-z0-9_-]{12,80}$"
          },
          "sessionId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "fileName": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "mimeType": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120
          },
          "byteSize": {
            "type": "integer",
            "minimum": 1
          },
          "sha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$"
          },
          "previewKind": {
            "type": "string",
            "enum": [
              "markdown",
              "code",
              "diff",
              "image",
              "html",
              "unsupported"
            ]
          },
          "language": {
            "type": "string",
            "maxLength": 40
          },
          "contentUrl": {
            "type": "string",
            "pattern": "^/api/agent/media/[^/]+/content\\?sessionId="
          }
        },
        "additionalProperties": false
      },
      "content": {
        "type": [
          "string",
          "null"
        ]
      },
      "previewByteSize": {
        "type": "integer",
        "minimum": 0,
        "maximum": 524288
      },
      "truncated": {
        "type": "boolean"
      }
    },
    "additionalProperties": false
  },
  "agent-goal-mutation.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-goal-mutation.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "action",
      "expectedRevision"
    ],
    "properties": {
      "action": {
        "type": "string",
        "enum": [
          "confirm_setup",
          "update",
          "pause",
          "resume",
          "complete",
          "cancel",
          "clear"
        ]
      },
      "expectedRevision": {
        "type": "integer",
        "minimum": 0
      },
      "confirmed": {
        "const": true
      },
      "objective": {
        "type": "string",
        "minLength": 1,
        "maxLength": 4000
      },
      "successCriteria": {
        "type": "string",
        "maxLength": 2000
      },
      "evidenceExpectations": {
        "type": "array",
        "maxItems": 20,
        "items": {
          "type": "string",
          "minLength": 1,
          "maxLength": 600
        }
      },
      "tokenBudget": {
        "type": [
          "integer",
          "null"
        ],
        "minimum": 1,
        "maximum": 100000000
      },
      "timeBudgetMs": {
        "type": [
          "integer",
          "null"
        ],
        "minimum": 1,
        "maximum": 31536000000
      },
      "summary": {
        "type": "string",
        "minLength": 1,
        "maxLength": 2000
      },
      "reason": {
        "type": "string",
        "minLength": 1,
        "maxLength": 1000
      },
      "evidence": {
        "type": "array",
        "minItems": 1,
        "maxItems": 20,
        "items": {
          "type": "object",
          "additionalProperties": false,
          "required": [
            "kind",
            "summary",
            "reference"
          ],
          "properties": {
            "kind": {
              "type": "string",
              "enum": [
                "test",
                "artifact",
                "commit",
                "receipt",
                "note"
              ]
            },
            "summary": {
              "type": "string",
              "minLength": 1,
              "maxLength": 600
            },
            "reference": {
              "type": "string",
              "minLength": 1,
              "maxLength": 1000
            }
          }
        }
      }
    },
    "allOf": [
      {
        "if": {
          "properties": {
            "action": {
              "const": "confirm_setup"
            }
          },
          "required": [
            "action"
          ]
        },
        "then": {
          "required": [
            "confirmed",
            "objective"
          ]
        }
      },
      {
        "if": {
          "properties": {
            "action": {
              "const": "complete"
            }
          },
          "required": [
            "action"
          ]
        },
        "then": {
          "required": [
            "summary",
            "evidence"
          ]
        }
      },
      {
        "if": {
          "properties": {
            "action": {
              "const": "cancel"
            }
          },
          "required": [
            "action"
          ]
        },
        "then": {
          "required": [
            "reason"
          ]
        }
      }
    ]
  },
  "agent-goal-settle-request.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-goal-settle-request.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "sessionId",
      "settleScopeId",
      "settleAttempt",
      "freshToolEvidenceCount",
      "freshToolEvidenceSha256"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.agent-goal-settle-request.v1"
      },
      "sessionId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "settleScopeId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "settleAttempt": {
        "type": "integer",
        "minimum": 1
      },
      "freshToolEvidenceCount": {
        "type": "integer",
        "minimum": 0,
        "maximum": 10000
      },
      "freshToolEvidenceSha256": {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$"
      }
    }
  },
  "agent-goal-settle-result.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-goal-settle-result.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "sessionId",
      "goalId",
      "goalRevision",
      "settleScopeId",
      "settleAttempt",
      "freshToolEvidenceCount",
      "freshToolEvidenceSha256",
      "continuationEpoch",
      "continuationCount",
      "continuationLimit",
      "continuationRemaining",
      "state",
      "reason",
      "followUpKey",
      "message"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.agent-goal-settle-result.v1"
      },
      "sessionId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "goalId": {
        "type": "string",
        "maxLength": 240
      },
      "goalRevision": {
        "type": "integer",
        "minimum": 0
      },
      "settleScopeId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "settleAttempt": {
        "type": "integer",
        "minimum": 1
      },
      "freshToolEvidenceCount": {
        "type": "integer",
        "minimum": 0,
        "maximum": 10000
      },
      "freshToolEvidenceSha256": {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$"
      },
      "continuationEpoch": {
        "type": "integer",
        "minimum": 1
      },
      "continuationCount": {
        "type": "integer",
        "minimum": 0
      },
      "continuationLimit": {
        "type": "integer",
        "minimum": 1
      },
      "continuationRemaining": {
        "type": "integer",
        "minimum": 0
      },
      "state": {
        "type": "string",
        "enum": [
          "inactive",
          "continue",
          "paused",
          "completed",
          "cancelled",
          "blocked",
          "stalled",
          "budget_exhausted"
        ]
      },
      "reason": {
        "type": "string",
        "minLength": 1,
        "maxLength": 120
      },
      "followUpKey": {
        "type": "string",
        "maxLength": 80
      },
      "message": {
        "type": "string",
        "maxLength": 2000
      }
    }
  },
  "agent-goal-usage.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-goal-usage.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "sessionId",
      "idempotencyKey"
    ],
    "anyOf": [
      {
        "required": [
          "tokenDelta"
        ]
      },
      {
        "required": [
          "elapsedDeltaMs"
        ]
      }
    ],
    "properties": {
      "sessionId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "turnId": {
        "type": "string",
        "maxLength": 240
      },
      "eventId": {
        "type": "string",
        "maxLength": 240
      },
      "idempotencyKey": {
        "type": "string",
        "minLength": 1,
        "maxLength": 300
      },
      "tokenDelta": {
        "type": "integer",
        "minimum": 0,
        "maximum": 10000000
      },
      "elapsedDeltaMs": {
        "type": "integer",
        "minimum": 0,
        "maximum": 604800000
      }
    }
  },
  "agent-lab-cost-receipt.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-lab-cost-receipt.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "authority",
      "pricingIdentity",
      "usage",
      "estimate",
      "billing",
      "boundaries",
      "receiptSha256"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.agent-lab-cost-receipt.v1"
      },
      "authority": {
        "enum": [
          "pricing_estimate",
          "pricing_lower_bound",
          "runtime_cost_reconciled"
        ]
      },
      "pricingIdentity": {
        "$ref": "#/$defs/pricingIdentity"
      },
      "usage": {
        "$ref": "#/$defs/usage"
      },
      "runtimeCostReceipt": {
        "$ref": "#/$defs/runtimeCostReceipt"
      },
      "pricingLowerBound": {
        "$ref": "#/$defs/pricingLowerBound"
      },
      "estimate": {
        "$ref": "#/$defs/estimate"
      },
      "billing": {
        "$ref": "#/$defs/billing"
      },
      "boundaries": {
        "type": "array",
        "minItems": 2,
        "maxItems": 8,
        "items": {
          "type": "string",
          "minLength": 1,
          "maxLength": 400
        }
      },
      "receiptSha256": {
        "$ref": "#/$defs/sha256"
      }
    },
    "allOf": [
      {
        "if": {
          "required": [
            "runtimeCostReceipt"
          ]
        },
        "then": {
          "properties": {
            "authority": {
              "const": "runtime_cost_reconciled"
            }
          }
        }
      },
      {
        "if": {
          "properties": {
            "authority": {
              "const": "runtime_cost_reconciled"
            }
          }
        },
        "then": {
          "required": [
            "runtimeCostReceipt"
          ]
        }
      },
      {
        "if": {
          "required": [
            "pricingLowerBound"
          ]
        },
        "then": {
          "properties": {
            "authority": {
              "const": "pricing_lower_bound"
            }
          }
        }
      },
      {
        "if": {
          "properties": {
            "authority": {
              "const": "pricing_lower_bound"
            }
          }
        },
        "then": {
          "required": [
            "pricingLowerBound"
          ]
        }
      }
    ],
    "$defs": {
      "sha256": {
        "type": "string",
        "pattern": "^[a-f0-9]{64}$"
      },
      "identity": {
        "type": "string",
        "minLength": 1,
        "maxLength": 200,
        "pattern": "^[A-Za-z0-9][A-Za-z0-9._:@/-]*$"
      },
      "evidenceRef": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240,
        "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$"
      },
      "rateDecimalUsd": {
        "type": "string",
        "minLength": 1,
        "maxLength": 40,
        "pattern": "^(0|[1-9][0-9]*)(\\.[0-9]+)?$"
      },
      "amountDecimalUsd": {
        "type": "string",
        "minLength": 1,
        "maxLength": 80,
        "pattern": "^(0|[1-9][0-9]*)(\\.[0-9]+)?$"
      },
      "rates": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "uncachedInputUsd",
          "cachedInputUsd",
          "outputUsd"
        ],
        "properties": {
          "uncachedInputUsd": {
            "$ref": "#/$defs/rateDecimalUsd"
          },
          "cachedInputUsd": {
            "$ref": "#/$defs/rateDecimalUsd"
          },
          "outputUsd": {
            "$ref": "#/$defs/rateDecimalUsd"
          }
        }
      },
      "pricingIdentity": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "pricingId",
          "provider",
          "model",
          "currency",
          "unit",
          "rates",
          "publishedDate",
          "sourceUrl",
          "sourceSha256"
        ],
        "properties": {
          "pricingId": {
            "$ref": "#/$defs/identity"
          },
          "provider": {
            "$ref": "#/$defs/identity"
          },
          "model": {
            "$ref": "#/$defs/identity"
          },
          "currency": {
            "const": "USD"
          },
          "unit": {
            "const": "per_million_tokens"
          },
          "rates": {
            "$ref": "#/$defs/rates"
          },
          "publishedDate": {
            "type": "string",
            "pattern": "^20[0-9]{2}-(0[1-9]|1[0-2])-([0-2][0-9]|3[01])$"
          },
          "sourceUrl": {
            "type": "string",
            "minLength": 9,
            "maxLength": 1000,
            "pattern": "^https://[^\\s]+$"
          },
          "sourceSha256": {
            "$ref": "#/$defs/sha256"
          }
        }
      },
      "usage": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "available",
          "uncachedInputTokens",
          "cachedInputTokens",
          "outputTokens",
          "sourceRef",
          "sourceSha256"
        ],
        "properties": {
          "available": {
            "const": true
          },
          "uncachedInputTokens": {
            "type": "integer",
            "minimum": 0,
            "maximum": 1000000000000000000
          },
          "cachedInputTokens": {
            "type": "integer",
            "minimum": 0,
            "maximum": 1000000000000000000
          },
          "outputTokens": {
            "type": "integer",
            "minimum": 0,
            "maximum": 1000000000000000000
          },
          "sourceRef": {
            "$ref": "#/$defs/evidenceRef"
          },
          "sourceSha256": {
            "$ref": "#/$defs/sha256"
          }
        }
      },
      "estimate": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "uncachedInputCostUsd",
          "cachedInputCostUsd",
          "outputCostUsd",
          "totalCostUsd"
        ],
        "properties": {
          "uncachedInputCostUsd": {
            "$ref": "#/$defs/amountDecimalUsd"
          },
          "cachedInputCostUsd": {
            "$ref": "#/$defs/amountDecimalUsd"
          },
          "outputCostUsd": {
            "$ref": "#/$defs/amountDecimalUsd"
          },
          "totalCostUsd": {
            "$ref": "#/$defs/amountDecimalUsd"
          }
        }
      },
      "runtimeCostReceipt": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "requestCount",
          "runtimeDbSha256",
          "transcriptSha256s",
          "databaseUsage",
          "reportedCostUsd",
          "sourceSha256"
        ],
        "properties": {
          "requestCount": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1000000
          },
          "runtimeDbSha256": {
            "$ref": "#/$defs/sha256"
          },
          "transcriptSha256s": {
            "type": "array",
            "minItems": 1,
            "maxItems": 10000,
            "items": {
              "$ref": "#/$defs/sha256"
            }
          },
          "databaseUsage": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "uncachedInputTokens",
              "cachedInputTokens",
              "outputTokens"
            ],
            "properties": {
              "uncachedInputTokens": {
                "type": "integer",
                "minimum": 0,
                "maximum": 1000000000000000000
              },
              "cachedInputTokens": {
                "type": "integer",
                "minimum": 0,
                "maximum": 1000000000000000000
              },
              "outputTokens": {
                "type": "integer",
                "minimum": 0,
                "maximum": 1000000000000000000
              }
            }
          },
          "trialAggregate": {
            "$ref": "#/$defs/trialAggregate"
          },
          "reportedCostUsd": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "input",
              "cacheRead",
              "output",
              "total"
            ],
            "properties": {
              "input": {
                "$ref": "#/$defs/amountDecimalUsd"
              },
              "cacheRead": {
                "$ref": "#/$defs/amountDecimalUsd"
              },
              "output": {
                "$ref": "#/$defs/amountDecimalUsd"
              },
              "total": {
                "$ref": "#/$defs/amountDecimalUsd"
              }
            }
          },
          "sourceSha256": {
            "$ref": "#/$defs/sha256"
          }
        }
      },
      "trialAggregate": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "sourceRef",
          "sourceSha256",
          "uncachedInputTokens",
          "cachedInputTokens",
          "outputTokens"
        ],
        "properties": {
          "sourceRef": {
            "$ref": "#/$defs/identity"
          },
          "sourceSha256": {
            "$ref": "#/$defs/sha256"
          },
          "uncachedInputTokens": {
            "type": "integer",
            "minimum": 0,
            "maximum": 1000000000000000000
          },
          "cachedInputTokens": {
            "type": "integer",
            "minimum": 0,
            "maximum": 1000000000000000000
          },
          "outputTokens": {
            "type": "integer",
            "minimum": 0,
            "maximum": 1000000000000000000
          }
        }
      },
      "pricingLowerBound": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "sourceSha256",
          "tiers"
        ],
        "properties": {
          "sourceSha256": {
            "$ref": "#/$defs/sha256"
          },
          "tiers": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": {
              "type": "object",
              "additionalProperties": false,
              "required": [
                "inputTokensAbove",
                "rates"
              ],
              "properties": {
                "inputTokensAbove": {
                  "type": "integer",
                  "minimum": 0,
                  "maximum": 1000000000000000000
                },
                "rates": {
                  "$ref": "#/$defs/rates"
                }
              }
            }
          }
        }
      },
      "billingNotProvided": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "status"
        ],
        "properties": {
          "status": {
            "const": "not_provided"
          }
        }
      },
      "billingProvided": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "status",
          "currency",
          "totalUsd",
          "receiptRef",
          "receiptSha256"
        ],
        "properties": {
          "status": {
            "const": "provided"
          },
          "currency": {
            "const": "USD"
          },
          "totalUsd": {
            "$ref": "#/$defs/amountDecimalUsd"
          },
          "receiptRef": {
            "$ref": "#/$defs/evidenceRef"
          },
          "receiptSha256": {
            "$ref": "#/$defs/sha256"
          }
        }
      },
      "billing": {
        "oneOf": [
          {
            "$ref": "#/$defs/billingNotProvided"
          },
          {
            "$ref": "#/$defs/billingProvided"
          }
        ]
      }
    }
  },
  "agent-lab-cost-request.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-lab-cost-request.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "pricingIdentity",
      "usage"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.agent-lab-cost-request.v1"
      },
      "pricingIdentity": {
        "$ref": "#/$defs/pricingIdentity"
      },
      "usage": {
        "$ref": "#/$defs/usage"
      },
      "runtimeCostReceipt": {
        "$ref": "#/$defs/runtimeCostReceipt"
      },
      "pricingLowerBound": {
        "$ref": "#/$defs/pricingLowerBound"
      },
      "billedReceipt": {
        "$ref": "#/$defs/billedReceipt"
      }
    },
    "$defs": {
      "sha256": {
        "type": "string",
        "pattern": "^[a-f0-9]{64}$"
      },
      "identity": {
        "type": "string",
        "minLength": 1,
        "maxLength": 200,
        "pattern": "^[A-Za-z0-9][A-Za-z0-9._:@/-]*$"
      },
      "evidenceRef": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240,
        "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$"
      },
      "decimalUsd": {
        "type": "string",
        "minLength": 1,
        "maxLength": 40,
        "pattern": "^(0|[1-9][0-9]*)(\\.[0-9]+)?$"
      },
      "rates": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "uncachedInputUsd",
          "cachedInputUsd",
          "outputUsd"
        ],
        "properties": {
          "uncachedInputUsd": {
            "$ref": "#/$defs/decimalUsd"
          },
          "cachedInputUsd": {
            "$ref": "#/$defs/decimalUsd"
          },
          "outputUsd": {
            "$ref": "#/$defs/decimalUsd"
          }
        }
      },
      "pricingIdentity": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "pricingId",
          "provider",
          "model",
          "currency",
          "unit",
          "rates",
          "publishedDate",
          "sourceUrl",
          "sourceSha256"
        ],
        "properties": {
          "pricingId": {
            "$ref": "#/$defs/identity"
          },
          "provider": {
            "$ref": "#/$defs/identity"
          },
          "model": {
            "$ref": "#/$defs/identity"
          },
          "currency": {
            "const": "USD"
          },
          "unit": {
            "const": "per_million_tokens"
          },
          "rates": {
            "$ref": "#/$defs/rates"
          },
          "publishedDate": {
            "type": "string",
            "pattern": "^20[0-9]{2}-(0[1-9]|1[0-2])-([0-2][0-9]|3[01])$"
          },
          "sourceUrl": {
            "type": "string",
            "minLength": 9,
            "maxLength": 1000,
            "pattern": "^https://[^\\s]+$"
          },
          "sourceSha256": {
            "$ref": "#/$defs/sha256"
          }
        }
      },
      "usage": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "available",
          "uncachedInputTokens",
          "cachedInputTokens",
          "outputTokens",
          "sourceRef",
          "sourceSha256"
        ],
        "properties": {
          "available": {
            "const": true
          },
          "uncachedInputTokens": {
            "type": "integer",
            "minimum": 0,
            "maximum": 1000000000000000000
          },
          "cachedInputTokens": {
            "type": "integer",
            "minimum": 0,
            "maximum": 1000000000000000000
          },
          "outputTokens": {
            "type": "integer",
            "minimum": 0,
            "maximum": 1000000000000000000
          },
          "sourceRef": {
            "$ref": "#/$defs/evidenceRef"
          },
          "sourceSha256": {
            "$ref": "#/$defs/sha256"
          }
        },
        "anyOf": [
          {
            "properties": {
              "uncachedInputTokens": {
                "minimum": 1
              }
            }
          },
          {
            "properties": {
              "cachedInputTokens": {
                "minimum": 1
              }
            }
          },
          {
            "properties": {
              "outputTokens": {
                "minimum": 1
              }
            }
          }
        ]
      },
      "runtimeCostReceipt": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "requestCount",
          "runtimeDbSha256",
          "transcriptSha256s",
          "databaseUsage",
          "reportedCostUsd",
          "sourceSha256"
        ],
        "properties": {
          "requestCount": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1000000
          },
          "runtimeDbSha256": {
            "$ref": "#/$defs/sha256"
          },
          "transcriptSha256s": {
            "type": "array",
            "minItems": 1,
            "maxItems": 10000,
            "items": {
              "$ref": "#/$defs/sha256"
            }
          },
          "databaseUsage": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "uncachedInputTokens",
              "cachedInputTokens",
              "outputTokens"
            ],
            "properties": {
              "uncachedInputTokens": {
                "type": "integer",
                "minimum": 0,
                "maximum": 1000000000000000000
              },
              "cachedInputTokens": {
                "type": "integer",
                "minimum": 0,
                "maximum": 1000000000000000000
              },
              "outputTokens": {
                "type": "integer",
                "minimum": 0,
                "maximum": 1000000000000000000
              }
            }
          },
          "trialAggregate": {
            "$ref": "#/$defs/trialAggregate"
          },
          "reportedCostUsd": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "input",
              "cacheRead",
              "output",
              "total"
            ],
            "properties": {
              "input": {
                "$ref": "#/$defs/decimalUsd"
              },
              "cacheRead": {
                "$ref": "#/$defs/decimalUsd"
              },
              "output": {
                "$ref": "#/$defs/decimalUsd"
              },
              "total": {
                "$ref": "#/$defs/decimalUsd"
              }
            }
          },
          "sourceSha256": {
            "$ref": "#/$defs/sha256"
          }
        }
      },
      "trialAggregate": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "sourceRef",
          "sourceSha256",
          "uncachedInputTokens",
          "cachedInputTokens",
          "outputTokens"
        ],
        "properties": {
          "sourceRef": {
            "$ref": "#/$defs/identity"
          },
          "sourceSha256": {
            "$ref": "#/$defs/sha256"
          },
          "uncachedInputTokens": {
            "type": "integer",
            "minimum": 0,
            "maximum": 1000000000000000000
          },
          "cachedInputTokens": {
            "type": "integer",
            "minimum": 0,
            "maximum": 1000000000000000000
          },
          "outputTokens": {
            "type": "integer",
            "minimum": 0,
            "maximum": 1000000000000000000
          }
        }
      },
      "pricingLowerBound": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "sourceSha256",
          "tiers"
        ],
        "properties": {
          "sourceSha256": {
            "$ref": "#/$defs/sha256"
          },
          "tiers": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": {
              "type": "object",
              "additionalProperties": false,
              "required": [
                "inputTokensAbove",
                "rates"
              ],
              "properties": {
                "inputTokensAbove": {
                  "type": "integer",
                  "minimum": 0,
                  "maximum": 1000000000000000000
                },
                "rates": {
                  "$ref": "#/$defs/rates"
                }
              }
            }
          }
        }
      },
      "billedReceipt": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "currency",
          "totalUsd",
          "receiptRef",
          "receiptSha256"
        ],
        "properties": {
          "currency": {
            "const": "USD"
          },
          "totalUsd": {
            "$ref": "#/$defs/decimalUsd"
          },
          "receiptRef": {
            "$ref": "#/$defs/evidenceRef"
          },
          "receiptSha256": {
            "$ref": "#/$defs/sha256"
          }
        }
      }
    }
  },
  "agent-lab-experiment.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-lab-experiment.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "experimentId",
      "revisionSha256",
      "title",
      "vertical",
      "evaluationKind",
      "status",
      "claimStatus",
      "businessProblem",
      "whyAgent",
      "dataset",
      "scoring",
      "factors",
      "frozenControls",
      "baseline",
      "candidate",
      "comparison",
      "star",
      "claim",
      "openGaps",
      "importedAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.agent-lab-experiment.v1"
      },
      "experimentId": {
        "type": "string",
        "pattern": "^[a-z0-9][a-z0-9._-]{0,159}$"
      },
      "revisionSha256": {
        "$ref": "#/$defs/sha256"
      },
      "title": {
        "$ref": "#/$defs/label"
      },
      "vertical": {
        "type": "string",
        "pattern": "^[a-z0-9][a-z0-9._-]{0,119}$"
      },
      "evaluationKind": {
        "enum": [
          "workflow",
          "rag_retrieval",
          "answer_evidence",
          "tool_runtime",
          "trace_repair",
          "memory",
          "model_cost",
          "other"
        ]
      },
      "status": {
        "enum": [
          "kept",
          "rejected",
          "diagnostic",
          "open_gap"
        ]
      },
      "claimStatus": {
        "enum": [
          "headline",
          "supporting",
          "diagnostic",
          "blocked"
        ]
      },
      "projectionState": {
        "enum": [
          "current",
          "history"
        ]
      },
      "supersededBy": {
        "type": "string",
        "pattern": "^[a-z0-9][a-z0-9._-]{0,159}$"
      },
      "effectStatus": {
        "enum": [
          "improved",
          "neutral",
          "regressed",
          "not_run",
          "unverified"
        ]
      },
      "candidateType": {
        "enum": [
          "single_factor",
          "compound_repair",
          "baseline",
          "unknown"
        ]
      },
      "businessProblem": {
        "$ref": "#/$defs/text"
      },
      "whyAgent": {
        "$ref": "#/$defs/text"
      },
      "dataset": {
        "$ref": "#/$defs/dataset"
      },
      "scoring": {
        "$ref": "#/$defs/scoring"
      },
      "factors": {
        "type": "array",
        "minItems": 1,
        "maxItems": 16,
        "items": {
          "$ref": "#/$defs/factor"
        }
      },
      "frozenControls": {
        "type": "array",
        "minItems": 1,
        "maxItems": 32,
        "items": {
          "$ref": "#/$defs/frozenControl"
        }
      },
      "baseline": {
        "$ref": "#/$defs/runSummary"
      },
      "candidate": {
        "$ref": "#/$defs/runSummary"
      },
      "comparison": {
        "$ref": "#/$defs/comparison"
      },
      "optimizationEvidence": {
        "$ref": "#/$defs/optimizationEvidence"
      },
      "star": {
        "$ref": "#/$defs/star"
      },
      "claim": {
        "$ref": "#/$defs/claim"
      },
      "openGaps": {
        "type": "array",
        "maxItems": 32,
        "items": {
          "$ref": "#/$defs/text"
        }
      },
      "importedAtMs": {
        "type": "integer",
        "minimum": 0
      }
    },
    "$defs": {
      "optimizationEvidence": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "status",
          "provenance",
          "patch",
          "baselineTrace",
          "caseComparisons",
          "validationBoundary",
          "gaps"
        ],
        "properties": {
          "status": {
            "enum": [
              "available",
              "partial",
              "unavailable"
            ]
          },
          "provenance": {
            "const": "existing_run_artifacts"
          },
          "patch": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "status",
              "kind",
              "artifactPath",
              "beforeRef",
              "afterRef",
              "unifiedDiff",
              "reason"
            ],
            "properties": {
              "status": {
                "enum": [
                  "available",
                  "unavailable"
                ]
              },
              "kind": {
                "const": "frozen_configuration"
              },
              "artifactPath": {
                "type": "string",
                "maxLength": 400
              },
              "beforeRef": {
                "type": "string",
                "maxLength": 400
              },
              "afterRef": {
                "type": "string",
                "maxLength": 400
              },
              "unifiedDiff": {
                "type": "string",
                "maxLength": 16000
              },
              "reason": {
                "$ref": "#/$defs/text"
              }
            }
          },
          "baselineTrace": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "runId",
              "status",
              "traceIds",
              "reason"
            ],
            "properties": {
              "runId": {
                "type": "string",
                "maxLength": 200
              },
              "status": {
                "enum": [
                  "bound",
                  "unavailable"
                ]
              },
              "traceIds": {
                "type": "array",
                "maxItems": 32,
                "items": {
                  "type": "string",
                  "pattern": "^trace:[a-zA-Z0-9:._-]{1,200}$"
                }
              },
              "reason": {
                "$ref": "#/$defs/text"
              }
            }
          },
          "caseComparisons": {
            "type": "array",
            "maxItems": 64,
            "items": {
              "type": "object",
              "additionalProperties": false,
              "required": [
                "caseId",
                "before",
                "after"
              ],
              "properties": {
                "caseId": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 120
                },
                "before": {
                  "$ref": "#/$defs/scoredCaseSummary"
                },
                "after": {
                  "$ref": "#/$defs/scoredCaseSummary"
                }
              }
            }
          },
          "validationBoundary": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "candidateAware",
              "candidateBlind",
              "heldOutOpened",
              "unbiasedPromotionClaimAllowed",
              "costAuthority"
            ],
            "properties": {
              "candidateAware": {
                "type": "boolean"
              },
              "candidateBlind": {
                "type": "boolean"
              },
              "heldOutOpened": {
                "type": "boolean"
              },
              "unbiasedPromotionClaimAllowed": {
                "type": "boolean"
              },
              "costAuthority": {
                "enum": [
                  "runtime_cost_reconciled",
                  "unavailable"
                ]
              }
            }
          },
          "gaps": {
            "type": "array",
            "maxItems": 16,
            "items": {
              "$ref": "#/$defs/text"
            }
          }
        }
      },
      "scoredCaseSummary": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "status",
          "metrics"
        ],
        "properties": {
          "status": {
            "enum": [
              "passed",
              "failed"
            ]
          },
          "metrics": {
            "$ref": "#/$defs/metrics"
          }
        }
      },
      "sha256": {
        "type": "string",
        "pattern": "^[a-f0-9]{64}$"
      },
      "label": {
        "type": "string",
        "minLength": 1,
        "maxLength": 200
      },
      "text": {
        "type": "string",
        "minLength": 1,
        "maxLength": 1600
      },
      "dataset": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "datasetId",
          "split",
          "caseCount",
          "unit",
          "manifestSha256",
          "heldOutConsumed"
        ],
        "properties": {
          "datasetId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200
          },
          "split": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80
          },
          "caseCount": {
            "type": "integer",
            "minimum": 0
          },
          "unit": {
            "$ref": "#/$defs/text"
          },
          "manifestSha256": {
            "$ref": "#/$defs/sha256"
          },
          "heldOutConsumed": {
            "type": "boolean"
          }
        }
      },
      "scoring": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "primaryMetric",
          "evaluatorAuthority",
          "goldHiddenFromAgent",
          "hardGates"
        ],
        "properties": {
          "primaryMetric": {
            "$ref": "#/$defs/text"
          },
          "evaluatorAuthority": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200
          },
          "goldHiddenFromAgent": {
            "type": "boolean"
          },
          "hardGates": {
            "type": "array",
            "maxItems": 32,
            "items": {
              "$ref": "#/$defs/text"
            }
          }
        }
      },
      "factor": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "name",
          "before",
          "after",
          "reason"
        ],
        "properties": {
          "name": {
            "enum": [
              "model",
              "prompt",
              "skill",
              "tool",
              "workflow",
              "context",
              "memory_rag",
              "guardrail",
              "execution_policy",
              "human_loop",
              "pricing"
            ]
          },
          "before": {
            "$ref": "#/$defs/text"
          },
          "after": {
            "$ref": "#/$defs/text"
          },
          "reason": {
            "$ref": "#/$defs/text"
          }
        }
      },
      "frozenControl": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "name",
          "value",
          "reason"
        ],
        "properties": {
          "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100
          },
          "value": {
            "$ref": "#/$defs/text"
          },
          "reason": {
            "$ref": "#/$defs/text"
          }
        }
      },
      "metrics": {
        "type": "object",
        "maxProperties": 64,
        "additionalProperties": {
          "type": "number"
        }
      },
      "runSummary": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "runId",
          "metrics",
          "evidenceRefs"
        ],
        "properties": {
          "runId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200
          },
          "metrics": {
            "$ref": "#/$defs/metrics"
          },
          "evidenceRefs": {
            "type": "array",
            "maxItems": 64,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 300
            }
          },
          "outputExamples": {
            "type": "array",
            "maxItems": 8,
            "items": {
              "$ref": "#/$defs/outputExample"
            }
          }
        }
      },
      "outputExample": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "caseId",
          "input",
          "output"
        ],
        "properties": {
          "caseId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120
          },
          "input": {
            "type": "string",
            "minLength": 1,
            "maxLength": 600
          },
          "output": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1200
          }
        }
      },
      "metricDelta": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "metric",
          "before",
          "after",
          "delta"
        ],
        "properties": {
          "metric": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100
          },
          "before": {
            "type": "number"
          },
          "after": {
            "type": "number"
          },
          "delta": {
            "type": "number"
          }
        }
      },
      "comparison": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "decision",
          "decisionReason",
          "metricDeltas"
        ],
        "properties": {
          "decision": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80
          },
          "decisionReason": {
            "$ref": "#/$defs/text"
          },
          "metricDeltas": {
            "type": "array",
            "maxItems": 64,
            "items": {
              "$ref": "#/$defs/metricDelta"
            }
          },
          "outputComparisons": {
            "type": "array",
            "maxItems": 8,
            "items": {
              "type": "object",
              "additionalProperties": false,
              "required": [
                "caseId",
                "before",
                "after"
              ],
              "properties": {
                "caseId": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 120
                },
                "before": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 1200
                },
                "after": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 1200
                }
              }
            }
          }
        }
      },
      "star": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "situation",
          "task",
          "action",
          "result"
        ],
        "properties": {
          "situation": {
            "$ref": "#/$defs/text"
          },
          "task": {
            "$ref": "#/$defs/text"
          },
          "action": {
            "$ref": "#/$defs/text"
          },
          "result": {
            "$ref": "#/$defs/text"
          }
        }
      },
      "claim": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "resumeBullet",
          "allowed",
          "forbidden"
        ],
        "properties": {
          "resumeBullet": {
            "$ref": "#/$defs/text"
          },
          "allowed": {
            "$ref": "#/$defs/text"
          },
          "forbidden": {
            "$ref": "#/$defs/text"
          }
        }
      }
    }
  },
  "agent-lab-path-search-request.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-lab-path-search-request.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "searchId",
      "title",
      "objective",
      "frozenControls",
      "baseline",
      "candidates"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.agent-lab-path-search-request.v1"
      },
      "searchId": {
        "type": "string",
        "pattern": "^[a-z0-9][a-z0-9._-]{0,159}$"
      },
      "title": {
        "type": "string",
        "minLength": 1,
        "maxLength": 200
      },
      "objective": {
        "$ref": "#/$defs/objective"
      },
      "frozenControls": {
        "type": "array",
        "minItems": 1,
        "maxItems": 32,
        "items": {
          "$ref": "#/$defs/frozenControl"
        }
      },
      "baseline": {
        "$ref": "#/$defs/node"
      },
      "candidates": {
        "type": "array",
        "minItems": 1,
        "maxItems": 128,
        "items": {
          "$ref": "#/$defs/node"
        }
      }
    },
    "$defs": {
      "text": {
        "type": "string",
        "minLength": 1,
        "maxLength": 1600
      },
      "sha256": {
        "type": "string",
        "pattern": "^[a-f0-9]{64}$"
      },
      "frozenControl": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "name",
          "value"
        ],
        "properties": {
          "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100
          },
          "value": {
            "$ref": "#/$defs/text"
          }
        }
      },
      "metricSpec": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "name",
          "direction",
          "weight",
          "class"
        ],
        "properties": {
          "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100
          },
          "direction": {
            "enum": [
              "max",
              "min"
            ]
          },
          "weight": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          },
          "class": {
            "enum": [
              "quality",
              "reliability",
              "efficiency",
              "cost"
            ]
          },
          "scale": {
            "type": "number",
            "exclusiveMinimum": 0
          },
          "nonRegression": {
            "type": "boolean"
          }
        }
      },
      "gateSpec": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "name",
          "metric",
          "operator",
          "value"
        ],
        "properties": {
          "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120
          },
          "metric": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100
          },
          "operator": {
            "enum": [
              "gte",
              "lte",
              "eq"
            ]
          },
          "value": {
            "type": "number"
          }
        }
      },
      "objective": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "userNeed",
          "metrics",
          "gates",
          "selectionPolicy"
        ],
        "properties": {
          "userNeed": {
            "$ref": "#/$defs/text"
          },
          "metrics": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "items": {
              "$ref": "#/$defs/metricSpec"
            }
          },
          "gates": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "items": {
              "$ref": "#/$defs/gateSpec"
            }
          },
          "selectionPolicy": {
            "enum": [
              "lexicographic_pareto",
              "weighted_pareto"
            ]
          }
        }
      },
      "node": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "nodeId",
          "parentNodeId",
          "changedFactor",
          "configRevision",
          "frozenControlHash",
          "metrics",
          "evidenceRefs",
          "status"
        ],
        "properties": {
          "nodeId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "parentNodeId": {
            "type": [
              "string",
              "null"
            ],
            "maxLength": 160
          },
          "changedFactor": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100
          },
          "configRevision": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200
          },
          "frozenControlHash": {
            "$ref": "#/$defs/sha256"
          },
          "metrics": {
            "type": "object",
            "maxProperties": 64,
            "additionalProperties": {
              "type": "number"
            }
          },
          "evidenceRefs": {
            "type": "array",
            "maxItems": 64,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 300
            }
          },
          "status": {
            "enum": [
              "eligible",
              "rejected",
              "not_evaluated",
              "unknown"
            ]
          },
          "reason": {
            "$ref": "#/$defs/text"
          }
        }
      }
    }
  },
  "agent-lab-path-search.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-lab-path-search.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "searchId",
      "title",
      "objective",
      "frozenControls",
      "baseline",
      "candidates",
      "selectedPath",
      "hardGates",
      "claim",
      "generatedAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.agent-lab-path-search.v1"
      },
      "searchId": {
        "type": "string",
        "pattern": "^[a-z0-9][a-z0-9._-]{0,159}$"
      },
      "title": {
        "type": "string",
        "minLength": 1,
        "maxLength": 200
      },
      "objective": {
        "$ref": "#/$defs/objective"
      },
      "frozenControls": {
        "type": "array",
        "minItems": 1,
        "maxItems": 32,
        "items": {
          "$ref": "#/$defs/frozenControl"
        }
      },
      "baseline": {
        "$ref": "#/$defs/node"
      },
      "candidates": {
        "type": "array",
        "minItems": 1,
        "maxItems": 128,
        "items": {
          "$ref": "#/$defs/node"
        }
      },
      "selectedPath": {
        "type": "array",
        "minItems": 1,
        "maxItems": 64,
        "items": {
          "$ref": "#/$defs/pathStep"
        }
      },
      "hardGates": {
        "type": "array",
        "minItems": 1,
        "maxItems": 32,
        "items": {
          "$ref": "#/$defs/gateResult"
        }
      },
      "claim": {
        "$ref": "#/$defs/claim"
      },
      "generatedAtMs": {
        "type": "integer",
        "minimum": 0
      }
    },
    "$defs": {
      "text": {
        "type": "string",
        "minLength": 1,
        "maxLength": 1600
      },
      "sha256": {
        "type": "string",
        "pattern": "^[a-f0-9]{64}$"
      },
      "frozenControl": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "name",
          "value"
        ],
        "properties": {
          "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100
          },
          "value": {
            "$ref": "#/$defs/text"
          }
        }
      },
      "metricSpec": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "name",
          "direction",
          "weight",
          "class"
        ],
        "properties": {
          "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100
          },
          "direction": {
            "enum": [
              "max",
              "min"
            ]
          },
          "weight": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          },
          "class": {
            "enum": [
              "quality",
              "reliability",
              "efficiency",
              "cost"
            ]
          },
          "scale": {
            "type": "number",
            "exclusiveMinimum": 0
          },
          "nonRegression": {
            "type": "boolean"
          }
        }
      },
      "gateSpec": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "name",
          "metric",
          "operator",
          "value"
        ],
        "properties": {
          "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120
          },
          "metric": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100
          },
          "operator": {
            "enum": [
              "gte",
              "lte",
              "eq"
            ]
          },
          "value": {
            "type": "number"
          }
        }
      },
      "objective": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "userNeed",
          "metrics",
          "gates",
          "selectionPolicy"
        ],
        "properties": {
          "userNeed": {
            "$ref": "#/$defs/text"
          },
          "metrics": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "items": {
              "$ref": "#/$defs/metricSpec"
            }
          },
          "gates": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "items": {
              "$ref": "#/$defs/gateSpec"
            }
          },
          "selectionPolicy": {
            "enum": [
              "lexicographic_pareto",
              "weighted_pareto"
            ]
          }
        }
      },
      "node": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "nodeId",
          "parentNodeId",
          "changedFactor",
          "configRevision",
          "frozenControlHash",
          "metrics",
          "evidenceRefs",
          "status"
        ],
        "properties": {
          "nodeId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "parentNodeId": {
            "type": [
              "string",
              "null"
            ],
            "maxLength": 160
          },
          "changedFactor": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100
          },
          "configRevision": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200
          },
          "frozenControlHash": {
            "$ref": "#/$defs/sha256"
          },
          "metrics": {
            "type": "object",
            "maxProperties": 64,
            "additionalProperties": {
              "type": "number"
            }
          },
          "evidenceRefs": {
            "type": "array",
            "maxItems": 64,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 300
            }
          },
          "status": {
            "enum": [
              "eligible",
              "rejected",
              "not_evaluated",
              "unknown"
            ]
          },
          "reason": {
            "$ref": "#/$defs/text"
          }
        }
      },
      "pathStep": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "nodeId",
          "decision",
          "reason"
        ],
        "properties": {
          "nodeId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "decision": {
            "enum": [
              "baseline",
              "keep",
              "reject",
              "not_evaluated",
              "unknown"
            ]
          },
          "reason": {
            "$ref": "#/$defs/text"
          }
        }
      },
      "gateResult": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "name",
          "status",
          "reason"
        ],
        "properties": {
          "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120
          },
          "status": {
            "enum": [
              "pass",
              "fail",
              "unknown"
            ]
          },
          "reason": {
            "$ref": "#/$defs/text"
          }
        }
      },
      "claim": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "status",
          "summary",
          "limitations"
        ],
        "properties": {
          "status": {
            "enum": [
              "best_known",
              "blocked",
              "insufficient_evidence"
            ]
          },
          "summary": {
            "$ref": "#/$defs/text"
          },
          "limitations": {
            "type": "array",
            "minItems": 1,
            "maxItems": 16,
            "items": {
              "$ref": "#/$defs/text"
            }
          }
        }
      }
    }
  },
  "agent-lifecycle-cancellation-audit.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-lifecycle-cancellation-audit.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "requestId",
      "sessionId",
      "scopeKind",
      "scopeId",
      "sourceRevision",
      "transitionRevision",
      "action",
      "reason",
      "state",
      "sourceTurnId",
      "owners",
      "createdAtMs",
      "updatedAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.agent-lifecycle-cancellation-audit.v1"
      },
      "requestId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "sessionId": {
        "type": "string",
        "minLength": 1
      },
      "scopeKind": {
        "const": "goal"
      },
      "scopeId": {
        "type": "string",
        "minLength": 1
      },
      "sourceRevision": {
        "type": "integer",
        "minimum": 0
      },
      "transitionRevision": {
        "type": "integer",
        "minimum": 1
      },
      "action": {
        "enum": [
          "cancel",
          "pause"
        ]
      },
      "reason": {
        "type": "string",
        "maxLength": 1000
      },
      "state": {
        "enum": [
          "pending",
          "completed",
          "partial",
          "unknown"
        ]
      },
      "sourceTurnId": {
        "type": "string",
        "maxLength": 240
      },
      "owners": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "runtime",
          "approval",
          "job",
          "delegation"
        ],
        "properties": {
          "runtime": {
            "$ref": "#/$defs/owner"
          },
          "approval": {
            "$ref": "#/$defs/owner"
          },
          "job": {
            "$ref": "#/$defs/owner"
          },
          "delegation": {
            "$ref": "#/$defs/owner"
          }
        }
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "updatedAtMs": {
        "type": "integer",
        "minimum": 0
      }
    },
    "$defs": {
      "owner": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "status",
          "receipt"
        ],
        "properties": {
          "status": {
            "enum": [
              "pending",
              "succeeded",
              "excluded",
              "partial",
              "unknown"
            ]
          },
          "receipt": {
            "type": "object"
          }
        }
      }
    }
  },
  "agent-media.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-media.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "mediaId",
      "ownerType",
      "ownerId",
      "mimeType",
      "byteSize",
      "sha256",
      "origin",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-media.v1"
      },
      "mediaId": {
        "type": "string",
        "minLength": 1
      },
      "ownerType": {
        "type": "string",
        "enum": [
          "session",
          "room"
        ]
      },
      "ownerId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "sessionId": {
        "type": "string",
        "minLength": 1
      },
      "roomId": {
        "type": "string",
        "minLength": 1
      },
      "fileName": {
        "type": "string",
        "maxLength": 160
      },
      "mimeType": {
        "type": "string",
        "enum": [
          "image/png",
          "image/jpeg",
          "image/gif",
          "image/webp",
          "audio/mpeg",
          "audio/mp4",
          "audio/wav",
          "application/pdf",
          "text/plain",
          "text/markdown",
          "text/html",
          "text/x-diff",
          "text/x-patch"
        ]
      },
      "byteSize": {
        "type": "integer",
        "minimum": 0
      },
      "sha256": {
        "type": "string",
        "minLength": 64
      },
      "width": {
        "type": [
          "integer",
          "null"
        ]
      },
      "height": {
        "type": [
          "integer",
          "null"
        ]
      },
      "durationMs": {
        "type": [
          "integer",
          "null"
        ]
      },
      "thumbnailMediaId": {
        "type": [
          "string",
          "null"
        ]
      },
      "origin": {
        "type": "string",
        "enum": [
          "user_attachment",
          "tool_result",
          "managed_asset"
        ]
      },
      "originTool": {
        "type": "string"
      },
      "originReceiptId": {
        "type": "string"
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      }
    }
  },
  "agent-memory-evidence.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-memory-evidence.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "evidenceId",
      "project",
      "roleId",
      "sessionId",
      "ownerKind",
      "ownerId",
      "knowledgeDomain",
      "scopeKind",
      "scopeId",
      "visibility",
      "authorizationRevision",
      "bindingId",
      "scopeMode",
      "sourceKind",
      "sourceId",
      "idempotencyKey",
      "text",
      "textSha256",
      "classification",
      "maySupportLongTermFact",
      "provenance",
      "metadata",
      "privacyClass",
      "status",
      "occurredAtMs",
      "recordedAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-memory-evidence.v1"
      },
      "evidenceId": {
        "type": "string",
        "minLength": 1
      },
      "project": {
        "type": "string"
      },
      "roleId": {
        "type": "string"
      },
      "sessionId": {
        "type": "string"
      },
      "ownerKind": {
        "type": "string",
        "minLength": 1
      },
      "ownerId": {
        "type": "string",
        "minLength": 1
      },
      "knowledgeDomain": {
        "type": "string",
        "minLength": 1
      },
      "scopeKind": {
        "type": "string",
        "minLength": 1
      },
      "scopeId": {
        "type": "string"
      },
      "visibility": {
        "type": "string",
        "minLength": 1
      },
      "authorizationRevision": {
        "type": "string"
      },
      "bindingId": {
        "type": "string"
      },
      "scopeMode": {
        "type": "string",
        "enum": [
          "legacy",
          "authoritative",
          "quarantined"
        ]
      },
      "sourceKind": {
        "type": "string",
        "enum": [
          "user_message",
          "assistant_message",
          "tool_receipt",
          "session_digest",
          "room_event",
          "work_receipt"
        ]
      },
      "sourceId": {
        "type": "string",
        "minLength": 1
      },
      "idempotencyKey": {
        "type": "string",
        "minLength": 1
      },
      "text": {
        "type": "string",
        "minLength": 1
      },
      "textSha256": {
        "type": "string",
        "minLength": 64
      },
      "classification": {
        "type": "string",
        "const": "raw_evidence"
      },
      "maySupportLongTermFact": {
        "type": "boolean",
        "const": false
      },
      "provenance": {
        "type": "object"
      },
      "metadata": {
        "type": "object"
      },
      "privacyClass": {
        "type": "string",
        "enum": [
          "local",
          "private"
        ]
      },
      "status": {
        "type": "string",
        "enum": [
          "active",
          "tombstoned"
        ]
      },
      "occurredAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "recordedAtMs": {
        "type": "integer",
        "minimum": 0
      }
    }
  },
  "agent-memory-maintenance-status.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-memory-maintenance-status.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "ok",
      "policy",
      "autoApply",
      "scheduledDraftOnly",
      "due",
      "dueReason",
      "idleMs",
      "compileState",
      "draftCoverage",
      "automation",
      "catalogConsolidation",
      "ownerCuration",
      "modelCuration",
      "bookProjection",
      "projection",
      "runs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-memory-maintenance-status.v1"
      },
      "ok": {
        "type": "boolean",
        "const": true
      },
      "policy": {
        "type": "string",
        "enum": [
          "review",
          "auto_governed",
          "disabled"
        ]
      },
      "autoApply": {
        "type": "boolean"
      },
      "scheduledDraftOnly": {
        "type": "boolean"
      },
      "due": {
        "type": "boolean"
      },
      "dueReason": {
        "type": "string",
        "enum": [
          "pending_events",
          "idle",
          "daily",
          "owner_daily",
          "owner_scheduled",
          "draft_pending_review",
          "automatic_organization_disabled",
          "not_due"
        ]
      },
      "idleMs": {
        "type": "integer",
        "minimum": 0
      },
      "compileState": {
        "type": "object",
        "required": [
          "project",
          "lastCompiledEventId",
          "lastRunMs",
          "pendingEventCount",
          "lastBundleHash"
        ],
        "properties": {
          "project": {
            "type": "string"
          },
          "lastCompiledEventId": {
            "type": "integer",
            "minimum": 0
          },
          "lastRunMs": {
            "type": "integer",
            "minimum": 0
          },
          "pendingEventCount": {
            "type": "integer",
            "minimum": 0
          },
          "lastBundleHash": {
            "type": "string"
          }
        }
      },
      "draftCoverage": {
        "type": "object",
        "required": [
          "coveredThroughEventId",
          "undraftedEventCount",
          "coversAllPending",
          "lastDraftRunId"
        ],
        "properties": {
          "coveredThroughEventId": {
            "type": "integer",
            "minimum": 0
          },
          "undraftedEventCount": {
            "type": "integer",
            "minimum": 0
          },
          "coversAllPending": {
            "type": "boolean"
          },
          "lastDraftRunId": {
            "type": "string"
          }
        }
      },
      "automation": {
        "type": "object",
        "required": [
          "minimumNewEvents",
          "idleThresholdMs",
          "dailyIntervalMs",
          "schedulerPollIntervalMs",
          "enabled",
          "model",
          "thinkingLevel",
          "runsPerDay",
          "autoApply",
          "curationProtocol",
          "targetSourceCount",
          "maximumSourceCount",
          "maximumInputTokens",
          "reservedContextTokens"
        ],
        "properties": {
          "minimumNewEvents": {
            "type": "integer",
            "minimum": 0
          },
          "idleThresholdMs": {
            "type": "integer",
            "minimum": 0
          },
          "dailyIntervalMs": {
            "type": "integer",
            "minimum": 0
          },
          "schedulerPollIntervalMs": {
            "type": "integer",
            "minimum": 0
          },
          "enabled": {
            "type": "boolean"
          },
          "model": {
            "type": "string"
          },
          "thinkingLevel": {
            "type": "string"
          },
          "runsPerDay": {
            "type": "integer",
            "minimum": 0
          },
          "autoApply": {
            "type": "boolean"
          },
          "curationProtocol": {
            "type": "string",
            "const": "atom-first-v1"
          },
          "targetSourceCount": {
            "type": "integer",
            "minimum": 1
          },
          "maximumSourceCount": {
            "type": "integer",
            "minimum": 1
          },
          "maximumInputTokens": {
            "type": "integer",
            "minimum": 1
          },
          "reservedContextTokens": {
            "type": "integer",
            "minimum": 1
          }
        }
      },
      "catalogConsolidation": {
        "type": "object",
        "required": [
          "schemaVersion",
          "project",
          "receiptId",
          "state",
          "lastAttemptAtMs",
          "lastCompletionAtMs",
          "nextDueAtMs",
          "catalogDigest",
          "lastSuccessfulCatalogDigest",
          "lastSuccessfulCatalogCommittedAtMs",
          "attemptCatalogDigest",
          "curationRunId",
          "attemptCount",
          "retryCount",
          "result",
          "error",
          "createdAtMs",
          "updatedAtMs",
          "leaseExpiresAtMs",
          "due",
          "dueReason",
          "observedAtMs",
          "enabled",
          "automaticOrganizationEnabled",
          "cadenceDays"
        ],
        "properties": {
          "schemaVersion": {
            "type": "string",
            "const": "rag-ime.memory-catalog-consolidation.v1"
          },
          "project": {
            "type": "string"
          },
          "enabled": {
            "type": "boolean"
          },
          "automaticOrganizationEnabled": {
            "type": "boolean"
          },
          "cadenceDays": {
            "type": "integer",
            "minimum": 1
          },
          "receiptId": {
            "type": "string"
          },
          "state": {
            "type": "string",
            "enum": [
              "never",
              "running",
              "completed",
              "failed"
            ]
          },
          "lastAttemptAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "lastCompletionAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "nextDueAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "catalogDigest": {
            "type": "string"
          },
          "lastSuccessfulCatalogDigest": {
            "type": "string"
          },
          "lastSuccessfulCatalogCommittedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "attemptCatalogDigest": {
            "type": "string"
          },
          "curationRunId": {
            "type": "string"
          },
          "attemptCount": {
            "type": "integer",
            "minimum": 0
          },
          "retryCount": {
            "type": "integer",
            "minimum": 0
          },
          "result": {
            "type": "object"
          },
          "error": {
            "type": "string"
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "updatedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "leaseExpiresAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "due": {
            "type": "boolean"
          },
          "dueReason": {
            "type": "string",
            "enum": [
              "first_run",
              "scheduled",
              "retry_backoff",
              "running",
              "lease_expired",
              "catalog_consolidation_disabled",
              "automatic_organization_disabled",
              "not_due"
            ]
          },
          "observedAtMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "pendingDraftCount": {
        "type": "integer",
        "minimum": 0
      },
      "ownerCuration": {
        "type": "object",
        "required": [
          "schemaVersion",
          "ok",
          "project",
          "policy",
          "due",
          "pendingSourceCount",
          "needsReviewSourceCount",
          "scopes"
        ],
        "properties": {
          "schemaVersion": {
            "type": "string",
            "const": "rag-ime.owner-memory-curation-status.v1"
          },
          "ok": {
            "type": "boolean",
            "const": true
          },
          "project": {
            "type": "string"
          },
          "policy": {
            "type": "object"
          },
          "due": {
            "type": "boolean"
          },
          "pendingSourceCount": {
            "type": "integer",
            "minimum": 0
          },
          "needsReviewSourceCount": {
            "type": "integer",
            "minimum": 0
          },
          "scopes": {
            "type": "array",
            "items": {
              "type": "object"
            }
          }
        }
      },
      "modelCuration": {
        "type": "object",
        "required": [
          "schemaVersion",
          "ok",
          "profile",
          "requiredModel",
          "requiredThinkingLevel",
          "minimumContextTokens",
          "stateCounts",
          "runs"
        ],
        "properties": {
          "schemaVersion": {
            "type": "string",
            "const": "rag-ime.memory-curation-model-status.v1"
          },
          "ok": {
            "type": "boolean",
            "const": true
          },
          "profile": {
            "type": "string",
            "const": "MEMORY_CURATION"
          },
          "requiredModel": {
            "type": "string",
            "const": "openai-codex/gpt-5.6-luna"
          },
          "requiredThinkingLevel": {
            "type": "string",
            "const": "max"
          },
          "minimumContextTokens": {
            "type": "integer",
            "minimum": 272000
          },
          "stateCounts": {
            "type": "object",
            "additionalProperties": {
              "type": "integer",
              "minimum": 0
            }
          },
          "runs": {
            "type": "array",
            "items": {
              "type": "object"
            }
          }
        }
      },
      "bookProjection": {
        "type": "object",
        "required": [
          "schemaVersion",
          "ok",
          "projectionOwner",
          "currentAtomCount",
          "desiredBookCount",
          "activeBookCount",
          "historicalBookCount",
          "unbookedAtomCount",
          "missingBookCount",
          "staleBookCount",
          "membershipMismatchCount",
          "guardedBookCount",
          "inSync"
        ],
        "properties": {
          "schemaVersion": {
            "type": "string",
            "const": "rag-ime.personal-memory-book-projection-status.v1"
          },
          "ok": {
            "type": "boolean"
          },
          "projectionOwner": {
            "type": "string"
          },
          "currentAtomCount": {
            "type": "integer",
            "minimum": 0
          },
          "desiredBookCount": {
            "type": "integer",
            "minimum": 0
          },
          "activeBookCount": {
            "type": "integer",
            "minimum": 0
          },
          "historicalBookCount": {
            "type": "integer",
            "minimum": 0
          },
          "unbookedAtomCount": {
            "type": "integer",
            "minimum": 0
          },
          "missingBookCount": {
            "type": "integer",
            "minimum": 0
          },
          "staleBookCount": {
            "type": "integer",
            "minimum": 0
          },
          "membershipMismatchCount": {
            "type": "integer",
            "minimum": 0
          },
          "guardedBookCount": {
            "type": "integer",
            "minimum": 0
          },
          "inSync": {
            "type": "boolean"
          }
        }
      },
      "projection": {
        "type": "object",
        "required": [
          "schemaVersion",
          "ok",
          "configured",
          "owner",
          "running",
          "lastRunAtMs",
          "lastError",
          "freshness",
          "disabledReason"
        ],
        "properties": {
          "schemaVersion": {
            "type": "string",
            "const": "rag-ime.memory-projection-runtime.v1"
          },
          "ok": {
            "type": "boolean"
          },
          "configured": {
            "type": "boolean"
          },
          "owner": {
            "type": "string"
          },
          "running": {
            "type": "boolean"
          },
          "lastRunAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "lastError": {
            "type": "string"
          },
          "freshness": {
            "type": "object"
          },
          "disabledReason": {
            "type": "string"
          }
        }
      },
      "runs": {
        "type": "array",
        "items": {
          "type": "object",
          "required": [
            "runId",
            "createdAtMs",
            "status",
            "summary",
            "diffCount",
            "bundleHash",
            "sourceCursor",
            "ownerKind",
            "ownerId",
            "runKind"
          ],
          "properties": {
            "runId": {
              "type": "string",
              "minLength": 1
            },
            "createdAtMs": {
              "type": "integer",
              "minimum": 0
            },
            "status": {
              "type": "string",
              "enum": [
                "draft",
                "applied",
                "partial",
                "rolled_back",
                "superseded",
                "dismissed",
                "empty"
              ]
            },
            "summary": {
              "type": "string"
            },
            "diffCount": {
              "type": "integer",
              "minimum": 0
            },
            "bundleHash": {
              "type": "string"
            },
            "sourceCursor": {
              "type": "object"
            },
            "ownerKind": {
              "type": "string",
              "enum": [
                "user",
                "shared",
                "agent",
                "session",
                "room"
              ]
            },
            "ownerId": {
              "type": "string",
              "minLength": 1
            },
            "runKind": {
              "type": "string",
              "enum": [
                "legacy",
                "daily_curation",
                "manual_curation",
                "dream_insight",
                "catalog_consolidation"
              ]
            }
          }
        }
      }
    }
  },
  "agent-memory-source.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-memory-source.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "sourceId",
      "sessionId",
      "piEntryId",
      "inputEventId",
      "sourceRole",
      "sourceRevision",
      "canonicalTextSha256",
      "status",
      "ownerKind",
      "ownerId",
      "knowledgeDomain",
      "scopeKind",
      "scopeId",
      "visibility",
      "authorizationRevision",
      "bindingId",
      "scopeMode",
      "roleId",
      "roleVersion",
      "sourceKind",
      "trustClass",
      "disposition",
      "dispositionReason",
      "curationRunId",
      "coverageStartEntryId",
      "coverageEndEntryId",
      "metadata",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-memory-source.v1"
      },
      "sourceId": {
        "type": "string",
        "minLength": 1
      },
      "sessionId": {
        "type": "string"
      },
      "piEntryId": {
        "type": "string",
        "minLength": 1
      },
      "inputEventId": {
        "type": "integer",
        "minimum": 1
      },
      "sourceRole": {
        "type": "string",
        "enum": [
          "user",
          "tool_receipt"
        ]
      },
      "sourceRevision": {
        "type": "integer",
        "minimum": 1
      },
      "canonicalTextSha256": {
        "type": "string",
        "minLength": 64
      },
      "status": {
        "type": "string",
        "enum": [
          "active",
          "superseded",
          "archived",
          "tombstoned"
        ]
      },
      "ownerKind": {
        "type": "string",
        "enum": [
          "user",
          "shared",
          "agent",
          "session",
          "room"
        ]
      },
      "ownerId": {
        "type": "string",
        "minLength": 1
      },
      "knowledgeDomain": {
        "type": "string",
        "minLength": 1
      },
      "scopeKind": {
        "type": "string",
        "minLength": 1
      },
      "scopeId": {
        "type": "string"
      },
      "visibility": {
        "type": "string",
        "minLength": 1
      },
      "authorizationRevision": {
        "type": "string"
      },
      "bindingId": {
        "type": "string"
      },
      "scopeMode": {
        "type": "string",
        "enum": [
          "legacy",
          "authoritative",
          "quarantined"
        ]
      },
      "roleId": {
        "type": "string"
      },
      "roleVersion": {
        "type": "string"
      },
      "sourceKind": {
        "type": "string",
        "enum": [
          "user_final",
          "tool_receipt",
          "session_compaction",
          "session_digest",
          "explicit_memory"
        ]
      },
      "trustClass": {
        "type": "string",
        "enum": [
          "user_claim",
          "applied_receipt",
          "session_summary",
          "assistant_claim",
          "explicit_command"
        ]
      },
      "disposition": {
        "type": "string",
        "enum": [
          "pending",
          "remember",
          "not_for_memory",
          "needs_review",
          "consolidated",
          "expired"
        ]
      },
      "dispositionReason": {
        "type": "string"
      },
      "dispositionUpdatedAtMs": {
        "type": [
          "integer",
          "null"
        ],
        "minimum": 0
      },
      "processedAtMs": {
        "type": [
          "integer",
          "null"
        ],
        "minimum": 0
      },
      "curationRunId": {
        "type": "string"
      },
      "coverageStartEntryId": {
        "type": "string"
      },
      "coverageEndEntryId": {
        "type": "string"
      },
      "expiresAtMs": {
        "type": [
          "integer",
          "null"
        ],
        "minimum": 0
      },
      "metadata": {
        "type": "object"
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "supersededAtMs": {
        "type": [
          "integer",
          "null"
        ]
      }
    }
  },
  "agent-message.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-message.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "id",
      "sessionId",
      "turnId",
      "role",
      "status",
      "blocks",
      "attachments",
      "citations",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-message.v1"
      },
      "id": {
        "type": "string",
        "minLength": 1
      },
      "sessionId": {
        "type": "string",
        "minLength": 1
      },
      "turnId": {
        "type": "string"
      },
      "role": {
        "type": "string",
        "enum": [
          "user",
          "assistant",
          "tool",
          "system"
        ]
      },
      "status": {
        "type": "string",
        "enum": [
          "queued",
          "streaming",
          "completed",
          "failed",
          "aborted"
        ]
      },
      "blocks": {
        "type": "array",
        "items": {
          "$ref": "#/$defs/block"
        }
      },
      "attachments": {
        "type": "array",
        "items": {
          "type": "string"
        }
      },
      "citations": {
        "type": "array",
        "items": {
          "type": "string"
        }
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "completedAtMs": {
        "type": [
          "integer",
          "null"
        ]
      },
      "clientMessageId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 128
      },
      "provider": {
        "type": "string"
      },
      "model": {
        "type": "string"
      },
      "usage": {
        "$ref": "#/$defs/usage"
      }
    },
    "$defs": {
      "usage": {
        "type": "object",
        "required": [
          "input",
          "output",
          "cacheRead",
          "cacheWrite",
          "totalTokens"
        ],
        "properties": {
          "input": {
            "type": "integer",
            "minimum": 0
          },
          "output": {
            "type": "integer",
            "minimum": 0
          },
          "cacheRead": {
            "type": "integer",
            "minimum": 0
          },
          "cacheWrite": {
            "type": "integer",
            "minimum": 0
          },
          "totalTokens": {
            "type": "integer",
            "minimum": 0
          }
        },
        "additionalProperties": false
      },
      "block": {
        "type": "object",
        "required": [
          "id",
          "type",
          "status",
          "presentationKind",
          "data"
        ],
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1
          },
          "schemaVersion": {
            "type": "string",
            "const": "rag-ime.agent-block.v1"
          },
          "type": {
            "type": "string",
            "enum": [
              "text",
              "code",
              "reasoning_summary",
              "progress",
              "tool_call",
              "tool_result",
              "citation",
              "image",
              "audio",
              "file",
              "sticker",
              "task_plan",
              "diff",
              "approval",
              "error",
              "card",
              "checklist",
              "table",
              "artifact",
              "reference",
              "status",
              "unknown"
            ]
          },
          "status": {
            "type": "string",
            "enum": [
              "queued",
              "running",
              "completed",
              "failed",
              "aborted"
            ]
          },
          "presentationKind": {
            "type": "string",
            "minLength": 1
          },
          "data": {
            "type": "object"
          },
          "summary": {
            "type": "string",
            "maxLength": 240
          },
          "source": {
            "type": "object",
            "required": [
              "kind",
              "ref"
            ],
            "properties": {
              "kind": {
                "type": "string",
                "maxLength": 80
              },
              "ref": {
                "type": "string",
                "maxLength": 240
              }
            },
            "additionalProperties": false
          },
          "visibility": {
            "type": "string",
            "enum": [
              "private_session",
              "room_post",
              "root_post"
            ]
          },
          "digest": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$"
          },
          "ref": {
            "type": "string",
            "maxLength": 240
          },
          "generation": {
            "type": "integer",
            "minimum": 0
          }
        }
      }
    }
  },
  "agent-model-catalog.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-model-catalog.v1",
    "$defs": {
      "model": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "provider",
          "id",
          "name",
          "api",
          "reasoning",
          "thinkingLevels",
          "supportsImages",
          "contextWindow",
          "maxTokens"
        ],
        "properties": {
          "provider": {
            "type": "string",
            "minLength": 1
          },
          "id": {
            "type": "string",
            "minLength": 1
          },
          "name": {
            "type": "string",
            "minLength": 1
          },
          "api": {
            "type": "string"
          },
          "reasoning": {
            "type": "boolean"
          },
          "thinkingLevels": {
            "type": "array",
            "minItems": 1,
            "items": {
              "type": "string",
              "enum": [
                "off",
                "minimal",
                "low",
                "medium",
                "high",
                "xhigh",
                "max"
              ]
            }
          },
          "supportsImages": {
            "type": "boolean"
          },
          "contextWindow": {
            "type": "integer",
            "minimum": 0
          },
          "maxTokens": {
            "type": "integer",
            "minimum": 0
          }
        }
      }
    },
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "ok",
      "sessionId",
      "selected",
      "thinkingLevel",
      "providers"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-model-catalog.v1"
      },
      "ok": {
        "type": "boolean",
        "const": true
      },
      "sessionId": {
        "type": "string",
        "minLength": 1
      },
      "selected": {
        "type": [
          "object",
          "null"
        ]
      },
      "thinkingLevel": {
        "type": "string",
        "enum": [
          "off",
          "minimal",
          "low",
          "medium",
          "high",
          "xhigh",
          "max"
        ]
      },
      "providers": {
        "type": "array",
        "items": {
          "type": "object",
          "additionalProperties": false,
          "required": [
            "id",
            "displayName",
            "models"
          ],
          "properties": {
            "id": {
              "type": "string",
              "minLength": 1
            },
            "displayName": {
              "type": "string",
              "minLength": 1
            },
            "models": {
              "type": "array",
              "items": {
                "$ref": "#/$defs/model"
              }
            }
          }
        }
      }
    }
  },
  "agent-model-selection.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-model-selection.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "ok",
      "sessionId",
      "selected",
      "session"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-model-selection.v1"
      },
      "ok": {
        "type": "boolean",
        "const": true
      },
      "sessionId": {
        "type": "string",
        "minLength": 1
      },
      "selected": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "provider",
          "id",
          "name",
          "api",
          "reasoning",
          "thinkingLevels",
          "supportsImages",
          "contextWindow",
          "maxTokens"
        ],
        "properties": {
          "provider": {
            "type": "string",
            "minLength": 1
          },
          "id": {
            "type": "string",
            "minLength": 1
          },
          "name": {
            "type": "string",
            "minLength": 1
          },
          "api": {
            "type": "string"
          },
          "reasoning": {
            "type": "boolean"
          },
          "thinkingLevels": {
            "type": "array",
            "minItems": 1,
            "items": {
              "type": "string",
              "enum": [
                "off",
                "minimal",
                "low",
                "medium",
                "high",
                "xhigh",
                "max"
              ]
            }
          },
          "supportsImages": {
            "type": "boolean"
          },
          "contextWindow": {
            "type": "integer",
            "minimum": 0
          },
          "maxTokens": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "session": {
        "type": "object",
        "required": [
          "schemaVersion",
          "id",
          "modelProfile"
        ],
        "properties": {
          "schemaVersion": {
            "type": "string",
            "const": "rag-ime.agent-session.v1"
          },
          "id": {
            "type": "string",
            "minLength": 1
          },
          "modelProfile": {
            "type": "string",
            "minLength": 1
          }
        }
      }
    }
  },
  "agent-participant.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-participant.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "id",
      "roomId",
      "sessionId",
      "roleId",
      "roleVersion",
      "displayName",
      "collaborationRole",
      "status",
      "ordinal",
      "createdAtMs",
      "lastSpokeAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-participant.v1"
      },
      "id": {
        "type": "string",
        "minLength": 1
      },
      "roomId": {
        "type": "string",
        "minLength": 1
      },
      "sessionId": {
        "type": "string",
        "minLength": 1
      },
      "roleId": {
        "type": "string",
        "minLength": 1
      },
      "roleVersion": {
        "type": "string",
        "minLength": 1
      },
      "displayName": {
        "type": "string",
        "minLength": 1,
        "maxLength": 40
      },
      "collaborationRole": {
        "type": "string",
        "enum": [
          "coordinator",
          "researcher",
          "implementer",
          "reviewer",
          "specialist"
        ]
      },
      "status": {
        "type": "string",
        "enum": [
          "active",
          "muted",
          "removed"
        ]
      },
      "ordinal": {
        "type": "integer",
        "minimum": 0,
        "maximum": 7
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "lastSpokeAtMs": {
        "type": [
          "integer",
          "null"
        ]
      }
    }
  },
  "agent-persona.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-persona.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "roleId",
      "version",
      "displayName",
      "tagline",
      "summary",
      "traits",
      "visualProfile",
      "defaults",
      "runtimeCharacteristics",
      "safetyPolicyVersion",
      "selectableModes"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-persona.v1"
      },
      "roleId": {
        "type": "string",
        "pattern": "^[a-z0-9][a-z0-9-]{1,62}$"
      },
      "version": {
        "type": "string",
        "pattern": "^[1-9][0-9]{0,5}$"
      },
      "displayName": {
        "type": "string",
        "minLength": 1,
        "maxLength": 40
      },
      "tagline": {
        "type": "string",
        "minLength": 1,
        "maxLength": 80
      },
      "summary": {
        "type": "string",
        "minLength": 1,
        "maxLength": 180
      },
      "traits": {
        "type": "array",
        "minItems": 1,
        "maxItems": 5,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "minLength": 1,
          "maxLength": 24
        }
      },
      "visualProfile": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "avatarAssetId",
          "symbolName",
          "accentToken"
        ],
        "properties": {
          "avatarAssetId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80
          },
          "symbolName": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80
          },
          "accentToken": {
            "type": "string",
            "enum": [
              "teal",
              "blue",
              "rose",
              "neutral"
            ]
          }
        }
      },
      "defaults": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "modelPolicy",
          "memoryPolicy",
          "toolProfileVersion"
        ],
        "properties": {
          "modelPolicy": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80
          },
          "memoryPolicy": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80
          },
          "toolProfileVersion": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80
          },
          "modelProfile": {
            "type": "string",
            "pattern": "^[^/\\s]{1,80}/\\S{1,160}$"
          },
          "thinkingLevel": {
            "type": "string",
            "enum": [
              "off",
              "minimal",
              "low",
              "medium",
              "high",
              "xhigh",
              "max"
            ]
          }
        }
      },
      "runtimeCharacteristics": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "intelligence",
          "speed",
          "context",
          "suitableTasks",
          "unsuitableTasks",
          "isDefault"
        ],
        "properties": {
          "intelligence": {
            "type": "string",
            "minLength": 1,
            "maxLength": 40
          },
          "speed": {
            "type": "string",
            "minLength": 1,
            "maxLength": 40
          },
          "context": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80
          },
          "suitableTasks": {
            "type": "array",
            "minItems": 1,
            "maxItems": 6,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 80
            }
          },
          "unsuitableTasks": {
            "type": "array",
            "minItems": 1,
            "maxItems": 6,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 80
            }
          },
          "isDefault": {
            "type": "boolean"
          }
        }
      },
      "safetyPolicyVersion": {
        "type": "string",
        "const": "agent-core-v2"
      },
      "selectableModes": {
        "type": "array",
        "minItems": 1,
        "maxItems": 2,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "enum": [
            "assistant",
            "coordinator"
          ]
        }
      }
    }
  },
  "agent-role-book-tool-result.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-role-book-tool-result.v1",
    "title": "Agent Role Book Tool Result",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "operation",
      "roleId",
      "roleVersion",
      "summary",
      "activeRevisionChanged",
      "result"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-role-book-tool-result.v1"
      },
      "operation": {
        "type": "string",
        "enum": [
          "get",
          "history",
          "propose_revision",
          "review"
        ]
      },
      "roleId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 120
      },
      "roleVersion": {
        "type": "string",
        "minLength": 1,
        "maxLength": 80
      },
      "summary": {
        "type": "string",
        "minLength": 1,
        "maxLength": 400
      },
      "activeRevisionChanged": {
        "type": "boolean",
        "const": false
      },
      "result": {
        "type": "object"
      }
    }
  },
  "agent-role-book.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-role-book.v1",
    "title": "Agent Role Book Revision",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "revisionId",
      "roleId",
      "roleVersion",
      "revisionNumber",
      "status",
      "displayName",
      "mission",
      "basePersonaVersion",
      "sections",
      "sourceRevisionId",
      "changeSummary",
      "proposedBy",
      "createdAtMs",
      "activatedAtMs",
      "supersededAtMs",
      "rolledBackAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-role-book.v1"
      },
      "revisionId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "roleId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 120
      },
      "roleVersion": {
        "type": "string",
        "minLength": 1,
        "maxLength": 80
      },
      "revisionNumber": {
        "type": "integer",
        "minimum": 1
      },
      "status": {
        "type": "string",
        "enum": [
          "draft",
          "active",
          "superseded",
          "rolled_back"
        ]
      },
      "displayName": {
        "type": "string",
        "maxLength": 80
      },
      "mission": {
        "type": "string",
        "maxLength": 400
      },
      "basePersonaVersion": {
        "type": "string",
        "maxLength": 120
      },
      "sections": {
        "$ref": "#/$defs/sections"
      },
      "sourceRevisionId": {
        "type": "string",
        "maxLength": 240
      },
      "changeSummary": {
        "type": "string",
        "maxLength": 400
      },
      "proposedBy": {
        "type": "string",
        "maxLength": 120
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "activatedAtMs": {
        "type": [
          "integer",
          "null"
        ],
        "minimum": 0
      },
      "supersededAtMs": {
        "type": [
          "integer",
          "null"
        ],
        "minimum": 0
      },
      "rolledBackAtMs": {
        "type": [
          "integer",
          "null"
        ],
        "minimum": 0
      }
    },
    "$defs": {
      "provenance": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "sourceType",
          "sourceId"
        ],
        "properties": {
          "sourceType": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80
          },
          "sourceId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "observedAtMs": {
            "type": [
              "integer",
              "null"
            ],
            "minimum": 0
          }
        }
      },
      "item": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "itemId",
          "text",
          "provenance",
          "evidenceIds"
        ],
        "properties": {
          "itemId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "text": {
            "type": "string",
            "minLength": 1,
            "maxLength": 280
          },
          "provenance": {
            "$ref": "#/$defs/provenance"
          },
          "expiresAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "evidenceIds": {
            "type": "array",
            "minItems": 1,
            "maxItems": 16,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 240
            }
          }
        }
      },
      "sections": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "personality",
          "capabilities",
          "recentWork",
          "lessonsAndLimits",
          "activeCommitments"
        ],
        "properties": {
          "personality": {
            "type": "array",
            "maxItems": 6,
            "items": {
              "$ref": "#/$defs/item"
            }
          },
          "capabilities": {
            "type": "array",
            "maxItems": 12,
            "items": {
              "$ref": "#/$defs/item"
            }
          },
          "recentWork": {
            "type": "array",
            "maxItems": 8,
            "items": {
              "$ref": "#/$defs/item"
            }
          },
          "lessonsAndLimits": {
            "type": "array",
            "maxItems": 8,
            "items": {
              "$ref": "#/$defs/item"
            }
          },
          "activeCommitments": {
            "type": "array",
            "maxItems": 8,
            "items": {
              "$ref": "#/$defs/item"
            }
          }
        }
      }
    }
  },
  "agent-role-routing-profile.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-role-routing-profile.v1",
    "title": "Agent Role Routing Profile",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "roleId",
      "roleVersion",
      "revisionId",
      "revisionNumber",
      "advisoryOnly",
      "personality",
      "capabilities",
      "recentWork",
      "activeCommitments",
      "lessonsAndLimits"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-role-routing-profile.v1"
      },
      "roleId": {
        "type": "string",
        "minLength": 1
      },
      "roleVersion": {
        "type": "string",
        "minLength": 1
      },
      "revisionId": {
        "type": "string",
        "minLength": 1
      },
      "revisionNumber": {
        "type": "integer",
        "minimum": 1
      },
      "advisoryOnly": {
        "type": "boolean"
      },
      "personality": {
        "type": "array",
        "items": {
          "$ref": "#/$defs/routingItem"
        }
      },
      "capabilities": {
        "type": "array",
        "items": {
          "$ref": "#/$defs/routingItem"
        }
      },
      "recentWork": {
        "type": "array",
        "items": {
          "$ref": "#/$defs/routingItem"
        }
      },
      "activeCommitments": {
        "type": "array",
        "items": {
          "$ref": "#/$defs/routingItem"
        }
      },
      "lessonsAndLimits": {
        "type": "array",
        "items": {
          "$ref": "#/$defs/routingItem"
        }
      }
    },
    "$defs": {
      "routingItem": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "itemId",
          "text",
          "provenance",
          "evidenceIds"
        ],
        "properties": {
          "itemId": {
            "type": "string",
            "minLength": 1
          },
          "text": {
            "type": "string",
            "minLength": 1
          },
          "expiresAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "provenance": {
            "type": "object",
            "required": [
              "sourceType",
              "sourceId"
            ],
            "properties": {
              "sourceType": {
                "type": "string",
                "minLength": 1
              },
              "sourceId": {
                "type": "string",
                "minLength": 1
              },
              "observedAtMs": {
                "type": [
                  "integer",
                  "null"
                ],
                "minimum": 0
              }
            }
          },
          "evidenceIds": {
            "type": "array",
            "items": {
              "type": "string",
              "minLength": 1
            }
          }
        }
      }
    }
  },
  "agent-room-conversation-snapshot.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-room-conversation-snapshot.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "ok",
      "room",
      "events",
      "firstEventSequence",
      "cursorSequence",
      "resumeToken",
      "deferredEventCount",
      "truncated"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-room-conversation-snapshot.v1"
      },
      "ok": {
        "type": "boolean",
        "const": true
      },
      "room": {
        "type": "object"
      },
      "events": {
        "type": "array",
        "maxItems": 2000,
        "items": {
          "type": "object"
        }
      },
      "firstEventSequence": {
        "type": "integer",
        "minimum": 0
      },
      "cursorSequence": {
        "type": "integer",
        "minimum": 0
      },
      "resumeToken": {
        "type": "string"
      },
      "deferredEventCount": {
        "type": "integer",
        "minimum": 0
      },
      "truncated": {
        "type": "boolean"
      }
    }
  },
  "agent-room-event-page.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-room-event-page.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "ok",
      "roomId",
      "items",
      "firstSequence",
      "lastSequence",
      "nextBeforeSequence",
      "hasMore",
      "retainedFirstSequence",
      "retainedLastSequence",
      "retainedPrefixTruncated"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-room-event-page.v1"
      },
      "ok": {
        "type": "boolean",
        "const": true
      },
      "roomId": {
        "type": "string",
        "minLength": 1
      },
      "items": {
        "type": "array",
        "maxItems": 200,
        "items": {
          "$ref": "#/$defs/event"
        }
      },
      "firstSequence": {
        "type": "integer",
        "minimum": 0
      },
      "lastSequence": {
        "type": "integer",
        "minimum": 0
      },
      "nextBeforeSequence": {
        "type": "integer",
        "minimum": 0
      },
      "hasMore": {
        "type": "boolean"
      },
      "retainedFirstSequence": {
        "type": "integer",
        "minimum": 0
      },
      "retainedLastSequence": {
        "type": "integer",
        "minimum": 0
      },
      "retainedPrefixTruncated": {
        "type": "boolean"
      }
    },
    "$defs": {
      "event": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "schemaVersion",
          "eventId",
          "roomId",
          "sequence",
          "turnId",
          "eventType",
          "participantId",
          "sourceSessionId",
          "createdAtMs",
          "payload",
          "resumeToken"
        ],
        "properties": {
          "schemaVersion": {
            "type": "string",
            "const": "rag-ime.agent-room-event.v1"
          },
          "eventId": {
            "type": "string",
            "minLength": 1
          },
          "roomId": {
            "type": "string",
            "minLength": 1
          },
          "sequence": {
            "type": "integer",
            "minimum": 1
          },
          "turnId": {
            "type": "string"
          },
          "eventType": {
            "type": "string",
            "enum": [
              "user_message",
              "route_decision",
              "participant_status",
              "participant_delta",
              "participant_activity",
              "participant_message",
              "room_post",
              "room_config_changed",
              "topic_changed",
              "artifact_changed",
              "turn_completed",
              "turn_failed",
              "snapshot_required",
              "room_start_confirmation_required",
              "room_start_confirmation_confirmed",
              "room_start_confirmation_rejected"
            ]
          },
          "participantId": {
            "type": [
              "string",
              "null"
            ]
          },
          "sourceSessionId": {
            "type": "string"
          },
          "topicId": {
            "type": "string"
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "payload": {
            "type": "object"
          },
          "resumeToken": {
            "type": "string",
            "minLength": 1
          }
        }
      }
    }
  },
  "agent-room-event.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-room-event.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "eventId",
      "roomId",
      "sequence",
      "turnId",
      "eventType",
      "participantId",
      "sourceSessionId",
      "createdAtMs",
      "payload",
      "resumeToken"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-room-event.v1"
      },
      "eventId": {
        "type": "string",
        "minLength": 1
      },
      "roomId": {
        "type": "string",
        "minLength": 1
      },
      "sequence": {
        "type": "integer",
        "minimum": 1
      },
      "turnId": {
        "type": "string"
      },
      "eventType": {
        "type": "string",
        "enum": [
          "user_message",
          "route_decision",
          "participant_status",
          "participant_delta",
          "participant_activity",
          "participant_message",
          "room_post",
          "room_config_changed",
          "topic_changed",
          "artifact_changed",
          "turn_completed",
          "turn_failed",
          "snapshot_required",
          "room_start_confirmation_required",
          "room_start_confirmation_confirmed",
          "room_start_confirmation_rejected"
        ]
      },
      "participantId": {
        "type": [
          "string",
          "null"
        ]
      },
      "sourceSessionId": {
        "type": "string"
      },
      "topicId": {
        "type": "string"
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "payload": {
        "type": "object"
      },
      "resumeToken": {
        "type": "string",
        "minLength": 1
      }
    }
  },
  "agent-room-intercom.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-room-intercom.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "id",
      "roomId",
      "kind",
      "sourceParticipantId",
      "targetParticipantId",
      "sourceSessionId",
      "targetSessionId",
      "sourceGeneration",
      "targetGeneration",
      "clientMessageId",
      "replyTo",
      "workItemId",
      "workAction",
      "status",
      "content",
      "acceptedTurnId",
      "error",
      "createdAtMs",
      "updatedAtMs",
      "deliveredAtMs",
      "repliedAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-room-intercom.v1"
      },
      "id": {
        "type": "string",
        "minLength": 1
      },
      "roomId": {
        "type": "string",
        "minLength": 1
      },
      "kind": {
        "type": "string",
        "enum": [
          "send",
          "ask",
          "reply"
        ]
      },
      "sourceParticipantId": {
        "type": "string",
        "minLength": 1
      },
      "targetParticipantId": {
        "type": "string",
        "minLength": 1
      },
      "sourceSessionId": {
        "type": "string",
        "minLength": 1
      },
      "targetSessionId": {
        "type": "string",
        "minLength": 1
      },
      "sourceGeneration": {
        "type": "integer",
        "minimum": 0
      },
      "targetGeneration": {
        "type": "integer",
        "minimum": 0
      },
      "clientMessageId": {
        "type": "string",
        "minLength": 1
      },
      "replyTo": {
        "type": "string"
      },
      "workItemId": {
        "type": "string"
      },
      "workAction": {
        "type": "string",
        "enum": [
          "",
          "assignment",
          "submission",
          "accepted",
          "revision",
          "blocked",
          "escalated"
        ]
      },
      "status": {
        "type": "string",
        "enum": [
          "queued",
          "delivering",
          "delivered",
          "replied",
          "failed",
          "stale",
          "cancelled"
        ]
      },
      "content": {
        "type": "string",
        "minLength": 1
      },
      "acceptedTurnId": {
        "type": "string"
      },
      "error": {
        "type": "string"
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "updatedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "deliveredAtMs": {
        "type": [
          "integer",
          "null"
        ]
      },
      "repliedAtMs": {
        "type": [
          "integer",
          "null"
        ]
      }
    }
  },
  "agent-room-snapshot.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-room-snapshot.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "ok",
      "room",
      "events",
      "firstSequence",
      "lastSequence",
      "resumeToken",
      "truncated"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-room-snapshot.v1"
      },
      "ok": {
        "type": "boolean",
        "const": true
      },
      "room": {
        "$ref": "#/$defs/room"
      },
      "events": {
        "type": "array",
        "maxItems": 2000,
        "items": {
          "$ref": "#/$defs/event"
        }
      },
      "firstSequence": {
        "type": "integer",
        "minimum": 0
      },
      "lastSequence": {
        "type": "integer",
        "minimum": 0
      },
      "resumeToken": {
        "type": "string"
      },
      "truncated": {
        "type": "boolean"
      }
    },
    "$defs": {
      "participant": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "schemaVersion",
          "id",
          "roomId",
          "sessionId",
          "roleId",
          "roleVersion",
          "displayName",
          "collaborationRole",
          "status",
          "ordinal",
          "createdAtMs",
          "lastSpokeAtMs"
        ],
        "properties": {
          "schemaVersion": {
            "type": "string",
            "const": "rag-ime.agent-participant.v1"
          },
          "id": {
            "type": "string",
            "minLength": 1
          },
          "roomId": {
            "type": "string",
            "minLength": 1
          },
          "sessionId": {
            "type": "string",
            "minLength": 1
          },
          "roleId": {
            "type": "string",
            "minLength": 1
          },
          "roleVersion": {
            "type": "string",
            "minLength": 1
          },
          "displayName": {
            "type": "string",
            "minLength": 1,
            "maxLength": 40
          },
          "collaborationRole": {
            "type": "string",
            "enum": [
              "coordinator",
              "researcher",
              "implementer",
              "reviewer",
              "specialist"
            ]
          },
          "status": {
            "type": "string",
            "enum": [
              "active",
              "muted",
              "removed"
            ]
          },
          "ordinal": {
            "type": "integer",
            "minimum": 0,
            "maximum": 7
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "lastSpokeAtMs": {
            "type": [
              "integer",
              "null"
            ]
          }
        }
      },
      "room": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "schemaVersion",
          "id",
          "title",
          "status",
          "executionMode",
          "permissionPolicy",
          "moderatorParticipantId",
          "workspaceRoots",
          "createdAtMs",
          "updatedAtMs",
          "lastEventSequence",
          "participants"
        ],
        "properties": {
          "schemaVersion": {
            "type": "string",
            "const": "rag-ime.agent-room.v1"
          },
          "id": {
            "type": "string",
            "minLength": 1
          },
          "title": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120
          },
          "status": {
            "type": "string",
            "enum": [
              "active",
              "archived"
            ]
          },
          "executionMode": {
            "type": "string",
            "enum": [
              "read_only",
              "per_action",
              "workspace_managed",
              "full_trust"
            ]
          },
          "permissionPolicy": {
            "$ref": "#/$defs/roomPermissionPolicy"
          },
          "roomKind": {
            "type": "string",
            "enum": [
              "collaboration",
              "roleplay"
            ]
          },
          "avatar": {
            "type": "string",
            "maxLength": 80
          },
          "description": {
            "type": "string",
            "maxLength": 500
          },
          "scenarioPrompt": {
            "type": "string",
            "maxLength": 8000
          },
          "ownerAppId": {
            "type": "string",
            "pattern": "^$|^extension:[a-z0-9][a-z0-9-]{0,63}$"
          },
          "surfaceKey": {
            "type": "string",
            "pattern": "^$|^[a-z0-9][a-z0-9._-]{0,63}$"
          },
          "routingPolicy": {
            "type": "string",
            "enum": [
              "manual_mentions",
              "moderator",
              "sequential",
              "natural",
              "parallel",
              "invite_only"
            ]
          },
          "routingConfig": {
            "type": "object"
          },
          "moderatorParticipantId": {
            "type": "string"
          },
          "nextSpeakerOrdinal": {
            "type": "integer",
            "minimum": 0,
            "maximum": 7
          },
          "activeTopicId": {
            "type": "string"
          },
          "configRevision": {
            "type": "integer",
            "minimum": 1
          },
          "workspaceRoots": {
            "type": "array",
            "maxItems": 5,
            "uniqueItems": true,
            "items": {
              "type": "string",
              "minLength": 1
            }
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "updatedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "lastEventSequence": {
            "type": "integer",
            "minimum": 0
          },
          "participants": {
            "type": "array",
            "minItems": 2,
            "maxItems": 8,
            "items": {
              "$ref": "#/$defs/participant"
            }
          },
          "topics": {
            "type": "array",
            "maxItems": 200,
            "items": {
              "type": "object"
            }
          },
          "artifacts": {
            "type": "array",
            "maxItems": 100,
            "items": {
              "type": "object"
            }
          },
          "workItems": {
            "type": "array",
            "maxItems": 100,
            "items": {
              "type": "object"
            }
          },
          "startGate": {
            "type": [
              "object",
              "null"
            ],
            "additionalProperties": true
          }
        }
      },
      "roomPermissionPolicy": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "schemaVersion",
          "room",
          "partner",
          "toolAgent"
        ],
        "properties": {
          "schemaVersion": {
            "type": "string",
            "const": "rag-ime.room-permission-policy.v1"
          },
          "room": {
            "$ref": "#/$defs/roomPermissionLayer"
          },
          "partner": {
            "$ref": "#/$defs/roomPermissionLowerLayer"
          },
          "toolAgent": {
            "$ref": "#/$defs/roomPermissionLowerLayer"
          }
        }
      },
      "roomPermissionLayer": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "executionMode"
        ],
        "properties": {
          "executionMode": {
            "type": "string",
            "enum": [
              "read_only",
              "per_action",
              "workspace_managed",
              "full_trust"
            ]
          }
        }
      },
      "roomPermissionLowerLayer": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "executionMode"
        ],
        "properties": {
          "executionMode": {
            "type": "string",
            "enum": [
              "inherit",
              "read_only",
              "per_action",
              "workspace_managed",
              "full_trust"
            ]
          }
        }
      },
      "event": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "schemaVersion",
          "eventId",
          "roomId",
          "sequence",
          "turnId",
          "eventType",
          "participantId",
          "sourceSessionId",
          "createdAtMs",
          "payload",
          "resumeToken"
        ],
        "properties": {
          "schemaVersion": {
            "type": "string",
            "const": "rag-ime.agent-room-event.v1"
          },
          "eventId": {
            "type": "string",
            "minLength": 1
          },
          "roomId": {
            "type": "string",
            "minLength": 1
          },
          "sequence": {
            "type": "integer",
            "minimum": 1
          },
          "turnId": {
            "type": "string"
          },
          "eventType": {
            "type": "string",
            "enum": [
              "user_message",
              "route_decision",
              "participant_status",
              "participant_delta",
              "participant_activity",
              "participant_message",
              "room_post",
              "room_config_changed",
              "topic_changed",
              "artifact_changed",
              "turn_completed",
              "turn_failed",
              "snapshot_required",
              "room_start_confirmation_required",
              "room_start_confirmation_confirmed",
              "room_start_confirmation_rejected"
            ]
          },
          "participantId": {
            "type": [
              "string",
              "null"
            ]
          },
          "sourceSessionId": {
            "type": "string"
          },
          "topicId": {
            "type": "string"
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "payload": {
            "type": "object"
          },
          "resumeToken": {
            "type": "string",
            "minLength": 1
          }
        }
      }
    }
  },
  "agent-room-work-item.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-room-work-item.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "id",
      "roomId",
      "topicId",
      "rootTurnId",
      "rootWorkId",
      "parentWorkId",
      "objective",
      "expectedOutput",
      "acceptanceCriteria",
      "accountableParticipantId",
      "currentOwnerParticipantId",
      "offeredToParticipantId",
      "createdByParticipantId",
      "clientMessageId",
      "assignmentKey",
      "state",
      "depth",
      "revision",
      "resultSummary",
      "artifactRefs",
      "evidenceRefs",
      "proposedOperabilityVerdict",
      "proposedRequirementVerdict",
      "review",
      "blocker",
      "acceptedTurnId",
      "createdAtMs",
      "updatedAtMs",
      "completedAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-room-work-item.v1"
      },
      "id": {
        "type": "string",
        "minLength": 1
      },
      "roomId": {
        "type": "string",
        "minLength": 1
      },
      "topicId": {
        "type": "string"
      },
      "rootTurnId": {
        "type": "string"
      },
      "rootWorkId": {
        "type": "string",
        "minLength": 1
      },
      "parentWorkId": {
        "type": "string"
      },
      "objective": {
        "type": "string",
        "minLength": 1,
        "maxLength": 4000
      },
      "expectedOutput": {
        "type": "string",
        "minLength": 1,
        "maxLength": 2000
      },
      "acceptanceCriteria": {
        "type": "array",
        "minItems": 1,
        "maxItems": 8,
        "items": {
          "type": "string",
          "minLength": 1,
          "maxLength": 500
        }
      },
      "accountableParticipantId": {
        "type": "string",
        "minLength": 1
      },
      "currentOwnerParticipantId": {
        "type": "string",
        "minLength": 1
      },
      "offeredToParticipantId": {
        "type": "string"
      },
      "createdByParticipantId": {
        "type": "string",
        "minLength": 1
      },
      "clientMessageId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 200
      },
      "assignmentKey": {
        "type": "string",
        "minLength": 1,
        "maxLength": 200
      },
      "state": {
        "type": "string",
        "enum": [
          "queued",
          "active",
          "review",
          "blocked",
          "done",
          "failed",
          "cancelled"
        ]
      },
      "depth": {
        "type": "integer",
        "minimum": 1,
        "maximum": 3
      },
      "revision": {
        "type": "integer",
        "minimum": 0,
        "maximum": 2
      },
      "resultSummary": {
        "type": "string",
        "maxLength": 4000
      },
      "artifactRefs": {
        "type": "array",
        "maxItems": 16,
        "items": {
          "type": "string",
          "minLength": 1,
          "maxLength": 1000
        }
      },
      "evidenceRefs": {
        "type": "array",
        "maxItems": 24,
        "items": {
          "type": "string",
          "minLength": 1,
          "maxLength": 1000
        }
      },
      "proposedOperabilityVerdict": {
        "type": "string",
        "enum": [
          "",
          "passed",
          "failed",
          "unverified"
        ]
      },
      "proposedRequirementVerdict": {
        "type": "string",
        "enum": [
          "",
          "satisfied",
          "not_satisfied",
          "unverified"
        ]
      },
      "review": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "operabilityVerdict",
          "requirementVerdict",
          "evidenceRefs",
          "reason",
          "reviewerParticipantId",
          "reviewedAtMs"
        ],
        "properties": {
          "operabilityVerdict": {
            "type": "string",
            "enum": [
              "",
              "passed",
              "failed",
              "unverified"
            ]
          },
          "requirementVerdict": {
            "type": "string",
            "enum": [
              "",
              "satisfied",
              "not_satisfied",
              "unverified"
            ]
          },
          "evidenceRefs": {
            "type": "array",
            "maxItems": 24,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 1000
            }
          },
          "reason": {
            "type": "string",
            "maxLength": 2000
          },
          "reviewerParticipantId": {
            "type": "string"
          },
          "reviewedAtMs": {
            "type": [
              "integer",
              "null"
            ],
            "minimum": 0
          }
        }
      },
      "blocker": {
        "type": "object"
      },
      "acceptedTurnId": {
        "type": "string"
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "updatedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "completedAtMs": {
        "type": [
          "integer",
          "null"
        ]
      }
    }
  },
  "agent-room.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-room.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "id",
      "title",
      "status",
      "routingPolicy",
      "moderatorParticipantId",
      "workspaceRoots",
      "executionMode",
      "permissionPolicy",
      "createdAtMs",
      "updatedAtMs",
      "lastEventSequence",
      "participants"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-room.v1"
      },
      "id": {
        "type": "string",
        "minLength": 1
      },
      "title": {
        "type": "string",
        "minLength": 1,
        "maxLength": 120
      },
      "status": {
        "type": "string",
        "enum": [
          "active",
          "archived"
        ]
      },
      "roomKind": {
        "type": "string",
        "enum": [
          "collaboration",
          "roleplay"
        ]
      },
      "avatar": {
        "type": "string",
        "maxLength": 80
      },
      "description": {
        "type": "string",
        "maxLength": 500
      },
      "scenarioPrompt": {
        "type": "string",
        "maxLength": 8000
      },
      "ownerAppId": {
        "type": "string",
        "pattern": "^$|^extension:[a-z0-9][a-z0-9-]{0,63}$"
      },
      "surfaceKey": {
        "type": "string",
        "pattern": "^$|^[a-z0-9][a-z0-9._-]{0,63}$"
      },
      "routingPolicy": {
        "type": "string",
        "enum": [
          "manual_mentions",
          "moderator",
          "sequential",
          "natural",
          "parallel",
          "invite_only"
        ]
      },
      "routingConfig": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "maxResponders",
          "naturalJitter",
          "fallbackParticipantId"
        ],
        "properties": {
          "maxResponders": {
            "type": "integer",
            "const": 1
          },
          "naturalJitter": {
            "type": "number",
            "minimum": 0,
            "maximum": 0.15
          },
          "fallbackParticipantId": {
            "type": "string"
          }
        }
      },
      "moderatorParticipantId": {
        "type": "string"
      },
      "nextSpeakerOrdinal": {
        "type": "integer",
        "minimum": 0,
        "maximum": 7
      },
      "activeTopicId": {
        "type": "string"
      },
      "configRevision": {
        "type": "integer",
        "minimum": 1
      },
      "workspaceRoots": {
        "type": "array",
        "maxItems": 5,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "permissionPolicy": {
        "$ref": "#/$defs/roomPermissionPolicy"
      },
      "executionMode": {
        "type": "string",
        "enum": [
          "read_only",
          "per_action",
          "workspace_managed",
          "full_trust"
        ]
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "updatedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "lastEventSequence": {
        "type": "integer",
        "minimum": 0
      },
      "participants": {
        "type": "array",
        "minItems": 2,
        "maxItems": 8,
        "items": {
          "type": "object"
        }
      },
      "topics": {
        "type": "array",
        "maxItems": 200,
        "items": {
          "type": "object"
        }
      },
      "artifacts": {
        "type": "array",
        "maxItems": 100,
        "items": {
          "type": "object"
        }
      },
      "workItems": {
        "type": "array",
        "maxItems": 100,
        "items": {
          "type": "object"
        }
      },
      "startGate": {
        "type": [
          "object",
          "null"
        ],
        "additionalProperties": true
      }
    },
    "$defs": {
      "roomPermissionPolicy": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "schemaVersion",
          "room",
          "partner",
          "toolAgent"
        ],
        "properties": {
          "schemaVersion": {
            "type": "string",
            "const": "rag-ime.room-permission-policy.v1"
          },
          "room": {
            "$ref": "#/$defs/roomPermissionLayer"
          },
          "partner": {
            "$ref": "#/$defs/roomPermissionLowerLayer"
          },
          "toolAgent": {
            "$ref": "#/$defs/roomPermissionLowerLayer"
          }
        }
      },
      "roomPermissionLayer": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "executionMode"
        ],
        "properties": {
          "executionMode": {
            "type": "string",
            "enum": [
              "read_only",
              "per_action",
              "workspace_managed",
              "full_trust"
            ]
          }
        }
      },
      "roomPermissionLowerLayer": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "executionMode"
        ],
        "properties": {
          "executionMode": {
            "type": "string",
            "enum": [
              "inherit",
              "read_only",
              "per_action",
              "workspace_managed",
              "full_trust"
            ]
          }
        }
      }
    }
  },
  "agent-runtime-binding.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-runtime-binding.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "driverId",
      "runtimeKind",
      "generation",
      "state",
      "createdAtMs",
      "updatedAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-runtime-binding.v1"
      },
      "sessionId": {
        "type": "string",
        "minLength": 1
      },
      "driverId": {
        "type": "string",
        "minLength": 1
      },
      "runtimeKind": {
        "type": "string",
        "minLength": 1
      },
      "externalSessionId": {
        "type": "string",
        "minLength": 1
      },
      "transcriptRef": {
        "type": "string"
      },
      "branchAnchor": {
        "type": "string"
      },
      "generation": {
        "type": "integer",
        "minimum": 1
      },
      "state": {
        "type": "string",
        "enum": [
          "prepared",
          "active",
          "stale"
        ]
      },
      "metadata": {
        "type": "object"
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "updatedAtMs": {
        "type": "integer",
        "minimum": 0
      }
    }
  },
  "agent-runtime.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-runtime.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "enabled",
      "managed",
      "status",
      "piVersion",
      "idleTimeoutSeconds",
      "capabilities"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-runtime.v1"
      },
      "enabled": {
        "type": "boolean"
      },
      "managed": {
        "type": "boolean"
      },
      "status": {
        "type": "string",
        "enum": [
          "disabled",
          "not_installed",
          "needs_configuration",
          "stopped",
          "starting",
          "ready",
          "busy",
          "faulted"
        ]
      },
      "driverId": {
        "type": "string",
        "minLength": 1
      },
      "runtimeKind": {
        "type": "string",
        "minLength": 1
      },
      "runtimeVersion": {
        "type": "string"
      },
      "piVersion": {
        "type": "string"
      },
      "idleTimeoutSeconds": {
        "type": "integer",
        "minimum": 0
      },
      "activeSessionId": {
        "type": [
          "string",
          "null"
        ]
      },
      "lastError": {
        "type": "string"
      },
      "capabilities": {
        "type": "object"
      }
    }
  },
  "agent-session-fork-candidates.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-session-fork-candidates.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "ok",
      "sessionId",
      "items"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-session-fork-candidates.v1"
      },
      "ok": {
        "type": "boolean",
        "const": true
      },
      "sessionId": {
        "type": "string",
        "minLength": 1
      },
      "items": {
        "type": "array",
        "maxItems": 500,
        "items": {
          "type": "object",
          "additionalProperties": false,
          "required": [
            "entryId",
            "text",
            "role",
            "createdAtMs"
          ],
          "properties": {
            "entryId": {
              "type": "string",
              "minLength": 1,
              "maxLength": 240
            },
            "text": {
              "type": "string",
              "minLength": 1,
              "maxLength": 8000
            },
            "role": {
              "type": "string",
              "enum": [
                "user",
                "assistant"
              ]
            },
            "createdAtMs": {
              "type": "integer",
              "minimum": 0
            }
          }
        }
      }
    }
  },
  "agent-session-fork-create.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-session-fork-create.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "ok",
      "sourceSessionId",
      "entryId",
      "selectedText",
      "session"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-session-fork-create.v1"
      },
      "ok": {
        "type": "boolean",
        "const": true
      },
      "sourceSessionId": {
        "type": "string",
        "minLength": 1
      },
      "entryId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "selectedText": {
        "type": "string",
        "maxLength": 8000
      },
      "session": {
        "$ref": "#/$defs/session"
      }
    },
    "$defs": {
      "session": {
        "type": "object",
        "required": [
          "schemaVersion",
          "id",
          "title",
          "mode",
          "status",
          "roleId",
          "roleVersion",
          "roleBookRevisionId",
          "modelProfile",
          "toolProfileVersion",
          "projectContextEnabled",
          "piSkillsEnabled",
          "codexSkillsEnabled",
          "createdAtMs",
          "updatedAtMs",
          "messageCount",
          "workspaceRoots"
        ],
        "properties": {
          "schemaVersion": {
            "type": "string",
            "const": "rag-ime.agent-session.v1"
          },
          "id": {
            "type": "string",
            "minLength": 1
          },
          "title": {
            "type": "string",
            "minLength": 1
          },
          "mode": {
            "type": "string",
            "enum": [
              "assistant",
              "coordinator"
            ]
          },
          "status": {
            "type": "string",
            "enum": [
              "idle",
              "active",
              "busy",
              "faulted",
              "archived"
            ]
          },
          "roleId": {
            "type": "string",
            "minLength": 1
          },
          "roleVersion": {
            "type": "string",
            "minLength": 1
          },
          "roleBookRevisionId": {
            "type": "string",
            "maxLength": 240
          },
          "modelProfile": {
            "type": "string",
            "minLength": 1
          },
          "toolProfileVersion": {
            "type": "string",
            "minLength": 1
          },
          "projectContextEnabled": {
            "type": "boolean"
          },
          "piSkillsEnabled": {
            "type": "boolean"
          },
          "codexSkillsEnabled": {
            "type": "boolean"
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "updatedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "messageCount": {
            "type": "integer",
            "minimum": 0
          },
          "workspaceRoots": {
            "type": "array",
            "items": {
              "type": "string"
            }
          }
        }
      }
    }
  },
  "agent-session-telemetry.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-session-telemetry.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "model",
      "context",
      "cumulativeUsage",
      "latestUsage",
      "latestCacheHitPercent",
      "isCompacting",
      "compactionCount",
      "updatedAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-session-telemetry.v1"
      },
      "model": {
        "type": "object",
        "required": [
          "provider",
          "id",
          "name"
        ],
        "properties": {
          "provider": {
            "type": "string"
          },
          "id": {
            "type": "string"
          },
          "name": {
            "type": "string"
          }
        },
        "additionalProperties": true
      },
      "context": {
        "type": "object",
        "required": [
          "tokens",
          "contextWindow",
          "percent",
          "remainingTokens",
          "compactAtTokens",
          "tokensUntilCompact",
          "reserveTokens",
          "keepRecentTokens",
          "autoCompactEnabled"
        ],
        "properties": {
          "tokens": {
            "type": [
              "integer",
              "null"
            ],
            "minimum": 0
          },
          "contextWindow": {
            "type": "integer",
            "minimum": 0
          },
          "percent": {
            "type": [
              "number",
              "null"
            ],
            "minimum": 0
          },
          "remainingTokens": {
            "type": [
              "integer",
              "null"
            ],
            "minimum": 0
          },
          "compactAtTokens": {
            "type": "integer",
            "minimum": 0
          },
          "tokensUntilCompact": {
            "type": [
              "integer",
              "null"
            ],
            "minimum": 0
          },
          "reserveTokens": {
            "type": "integer",
            "minimum": 0
          },
          "keepRecentTokens": {
            "type": "integer",
            "minimum": 0
          },
          "autoCompactEnabled": {
            "type": "boolean"
          }
        },
        "additionalProperties": false
      },
      "cumulativeUsage": {
        "$ref": "#/$defs/usage"
      },
      "latestUsage": {
        "$ref": "#/$defs/usage"
      },
      "latestCacheHitPercent": {
        "type": [
          "number",
          "null"
        ],
        "minimum": 0,
        "maximum": 100
      },
      "isCompacting": {
        "type": "boolean"
      },
      "compactionCount": {
        "type": "integer",
        "minimum": 0
      },
      "latestCompaction": {
        "type": "object",
        "required": [
          "reason",
          "status",
          "updatedAtMs"
        ],
        "properties": {
          "reason": {
            "type": "string",
            "enum": [
              "manual",
              "threshold",
              "overflow"
            ]
          },
          "status": {
            "type": "string",
            "enum": [
              "running",
              "completed",
              "failed",
              "aborted"
            ]
          },
          "tokensBefore": {
            "type": "integer",
            "minimum": 0
          },
          "estimatedTokensAfter": {
            "type": "integer",
            "minimum": 0
          },
          "willRetry": {
            "type": "boolean"
          },
          "error": {
            "type": "string"
          },
          "updatedAtMs": {
            "type": "integer",
            "minimum": 0
          }
        },
        "additionalProperties": false
      },
      "updatedAtMs": {
        "type": "integer",
        "minimum": 0
      }
    },
    "$defs": {
      "usage": {
        "type": "object",
        "required": [
          "input",
          "output",
          "cacheRead",
          "cacheWrite",
          "totalTokens"
        ],
        "properties": {
          "input": {
            "type": "integer",
            "minimum": 0
          },
          "output": {
            "type": "integer",
            "minimum": 0
          },
          "cacheRead": {
            "type": "integer",
            "minimum": 0
          },
          "cacheWrite": {
            "type": "integer",
            "minimum": 0
          },
          "totalTokens": {
            "type": "integer",
            "minimum": 0
          }
        },
        "additionalProperties": false
      }
    },
    "additionalProperties": false
  },
  "agent-session.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-session.v1",
    "type": "object",
    "allOf": [
      {
        "if": {
          "properties": {
            "surfaceKind": {
              "const": "agent"
            }
          },
          "required": [
            "surfaceKind"
          ]
        },
        "then": {
          "properties": {
            "ownerAppId": {
              "const": ""
            },
            "surfaceKey": {
              "const": ""
            }
          }
        }
      },
      {
        "if": {
          "properties": {
            "surfaceKind": {
              "const": "extension_app"
            }
          },
          "required": [
            "surfaceKind"
          ]
        },
        "then": {
          "properties": {
            "ownerAppId": {
              "pattern": "^extension:[a-z0-9][a-z0-9-]{0,63}$"
            },
            "surfaceKey": {
              "pattern": "^[a-z0-9][a-z0-9._-]{0,63}$"
            }
          }
        }
      },
      {
        "if": {
          "properties": {
            "surfaceKind": {
              "const": "builtin_app"
            }
          },
          "required": [
            "surfaceKind"
          ]
        },
        "then": {
          "properties": {
            "ownerAppId": {
              "const": "memory"
            },
            "surfaceKey": {
              "pattern": "^(?:timeline|journal-[0-9]{4}-[0-9]{2}-[0-9]{2})$"
            }
          }
        }
      }
    ],
    "required": [
      "schemaVersion",
      "id",
      "title",
      "mode",
      "status",
      "roleId",
      "roleVersion",
      "roleBookRevisionId",
      "modelProfile",
      "toolProfileVersion",
      "executionMode",
      "workspaceScopeGranted",
      "workspaceScopeSha256",
      "workspaceScopeGrantedAtMs",
      "capabilityDisclosurePreferences",
      "policyRevision",
      "projectContextEnabled",
      "piSkillsEnabled",
      "codexSkillsEnabled",
      "createdAtMs",
      "updatedAtMs",
      "messageCount",
      "workspaceRoots"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-session.v1"
      },
      "id": {
        "type": "string",
        "minLength": 1
      },
      "piSessionId": {
        "type": "string"
      },
      "sessionFile": {
        "type": "string"
      },
      "runtimeBinding": {
        "type": "object",
        "required": [
          "schemaVersion",
          "driverId",
          "runtimeKind",
          "generation",
          "state",
          "createdAtMs",
          "updatedAtMs"
        ],
        "properties": {
          "schemaVersion": {
            "type": "string",
            "const": "rag-ime.agent-runtime-binding.v1"
          },
          "driverId": {
            "type": "string",
            "minLength": 1
          },
          "runtimeKind": {
            "type": "string",
            "minLength": 1
          },
          "generation": {
            "type": "integer",
            "minimum": 1
          },
          "state": {
            "type": "string",
            "enum": [
              "prepared",
              "active",
              "stale"
            ]
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "updatedAtMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "title": {
        "type": "string",
        "minLength": 1
      },
      "mode": {
        "type": "string",
        "enum": [
          "assistant",
          "coordinator"
        ]
      },
      "status": {
        "type": "string",
        "enum": [
          "idle",
          "active",
          "busy",
          "faulted",
          "archived"
        ]
      },
      "sessionKind": {
        "type": "string",
        "enum": [
          "conversation",
          "subagent_runtime"
        ]
      },
      "evaluationSnapshot": {
        "type": "boolean"
      },
      "surfaceKind": {
        "type": "string",
        "enum": [
          "agent",
          "extension_app",
          "builtin_app"
        ]
      },
      "ownerAppId": {
        "type": "string",
        "pattern": "^$|^memory$|^extension:[a-z0-9][a-z0-9-]{0,63}$"
      },
      "surfaceKey": {
        "type": "string",
        "pattern": "^$|^[a-z0-9][a-z0-9._-]{0,63}$"
      },
      "roomParticipant": {
        "type": "object",
        "required": [
          "roomId",
          "participantId",
          "status"
        ],
        "properties": {
          "roomId": {
            "type": "string",
            "minLength": 1
          },
          "participantId": {
            "type": "string",
            "minLength": 1
          },
          "status": {
            "type": "string",
            "enum": [
              "active",
              "muted",
              "removed"
            ]
          }
        },
        "additionalProperties": false
      },
      "roleId": {
        "type": "string",
        "minLength": 1
      },
      "roleVersion": {
        "type": "string",
        "minLength": 1
      },
      "roleBookRevisionId": {
        "type": "string",
        "maxLength": 240
      },
      "modelProfile": {
        "type": "string",
        "minLength": 1
      },
      "thinkingLevel": {
        "type": "string",
        "enum": [
          "",
          "off",
          "minimal",
          "low",
          "medium",
          "high",
          "xhigh",
          "max"
        ]
      },
      "toolProfileVersion": {
        "type": "string",
        "minLength": 1
      },
      "executionMode": {
        "type": "string",
        "enum": [
          "read_only",
          "per_action",
          "workspace_managed",
          "full_trust"
        ]
      },
      "roomExecutionMode": {
        "type": "string",
        "enum": [
          "",
          "room_unrestricted"
        ]
      },
      "workspaceScopeGranted": {
        "type": "boolean"
      },
      "workspaceScopeSha256": {
        "type": "string",
        "pattern": "^$|^[a-f0-9]{64}$"
      },
      "workspaceScopeGrantedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "toolAllowlistMode": {
        "type": "string",
        "enum": [
          "profile",
          "explicit"
        ]
      },
      "allowedTools": {
        "type": "array",
        "items": {
          "type": "string",
          "minLength": 1
        },
        "uniqueItems": true
      },
      "capabilityDisclosurePreferences": {
        "type": "object",
        "additionalProperties": {
          "type": "string",
          "enum": [
            "inherit",
            "enabled",
            "disabled"
          ]
        }
      },
      "policyRevision": {
        "type": "integer",
        "minimum": 1
      },
      "projectContextEnabled": {
        "type": "boolean"
      },
      "piSkillsEnabled": {
        "type": "boolean"
      },
      "codexSkillsEnabled": {
        "type": "boolean"
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "updatedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "lastOpenedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "archivedAtMs": {
        "type": [
          "integer",
          "null"
        ]
      },
      "messageCount": {
        "type": "integer",
        "minimum": 0
      },
      "lastMessagePreview": {
        "type": "string"
      },
      "lastTerminalTurnId": {
        "type": "string",
        "maxLength": 240
      },
      "workspaceRoots": {
        "type": "array",
        "items": {
          "type": "string"
        }
      },
      "shellPolicyVersion": {
        "type": "string"
      }
    }
  },
  "agent-subagent-batch.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-subagent-batch.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "id",
      "parentSessionId",
      "parentRunId",
      "contextMode",
      "resultDeliveryMode",
      "state",
      "depth",
      "maxDepth",
      "abortRequested",
      "causalMetadata",
      "createdAtMs",
      "updatedAtMs",
      "completedAtMs",
      "runs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-subagent-batch.v1"
      },
      "id": {
        "type": "string",
        "minLength": 1
      },
      "parentSessionId": {
        "type": "string",
        "minLength": 1
      },
      "parentRunId": {
        "type": "string"
      },
      "contextMode": {
        "type": "string",
        "enum": [
          "fresh",
          "fork"
        ]
      },
      "resultDeliveryMode": {
        "type": "string",
        "enum": [
          "inline",
          "next_turn"
        ]
      },
      "state": {
        "type": "string",
        "enum": [
          "queued",
          "running",
          "completed",
          "failed",
          "aborted",
          "timed_out"
        ]
      },
      "depth": {
        "type": "integer",
        "minimum": 1,
        "maximum": 2
      },
      "maxDepth": {
        "type": "integer",
        "minimum": 1,
        "maximum": 2
      },
      "abortRequested": {
        "type": "boolean"
      },
      "causalMetadata": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "todoId",
          "todoRevision",
          "goalId",
          "goalRevision",
          "roomBound",
          "roomId",
          "rootId",
          "taskId",
          "dispatchId",
          "generation"
        ],
        "properties": {
          "todoId": {
            "type": "string",
            "maxLength": 240
          },
          "todoRevision": {
            "type": "integer",
            "minimum": 0
          },
          "goalId": {
            "type": "string",
            "maxLength": 240
          },
          "goalRevision": {
            "type": "integer",
            "minimum": 0
          },
          "roomBound": {
            "type": "boolean"
          },
          "roomId": {
            "type": "string",
            "maxLength": 240
          },
          "rootId": {
            "type": "string",
            "maxLength": 240
          },
          "taskId": {
            "type": "string",
            "maxLength": 240
          },
          "dispatchId": {
            "type": "string",
            "maxLength": 240
          },
          "generation": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "updatedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "completedAtMs": {
        "type": [
          "integer",
          "null"
        ],
        "minimum": 0
      },
      "runs": {
        "type": "array",
        "minItems": 1,
        "maxItems": 2,
        "items": {
          "type": "object"
        }
      }
    }
  },
  "agent-subagent-run.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-subagent-run.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "id",
      "nodeId",
      "attemptId",
      "attemptNumber",
      "predecessorAttemptId",
      "ownerRunId",
      "parentRunId",
      "depth",
      "batchId",
      "childSessionId",
      "todoTask",
      "todoPhase",
      "templateId",
      "templateVersion",
      "ordinal",
      "task",
      "expectedOutput",
      "acceptanceCriteria",
      "launchDigest",
      "contract",
      "state",
      "budget",
      "usage",
      "result",
      "error",
      "resultContextScheduledAtMs",
      "createdAtMs",
      "startedAtMs",
      "updatedAtMs",
      "completedAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-subagent-run.v1"
      },
      "id": {
        "type": "string",
        "minLength": 1
      },
      "nodeId": {
        "type": "string",
        "minLength": 1
      },
      "attemptId": {
        "type": "string",
        "minLength": 1
      },
      "attemptNumber": {
        "type": "integer",
        "minimum": 1
      },
      "predecessorAttemptId": {
        "type": "string"
      },
      "ownerRunId": {
        "type": "string",
        "minLength": 1
      },
      "parentRunId": {
        "type": "string"
      },
      "depth": {
        "type": "integer",
        "minimum": 1,
        "maximum": 2
      },
      "batchId": {
        "type": "string",
        "minLength": 1
      },
      "childSessionId": {
        "type": "string",
        "minLength": 1
      },
      "todoTask": {
        "type": "string",
        "maxLength": 240
      },
      "todoPhase": {
        "type": "string",
        "maxLength": 80
      },
      "templateId": {
        "type": "string",
        "enum": [
          "researcher",
          "planner",
          "worker",
          "reviewer",
          "delegate"
        ]
      },
      "templateVersion": {
        "type": "string",
        "const": "1"
      },
      "ordinal": {
        "type": "integer",
        "minimum": 0,
        "maximum": 1
      },
      "task": {
        "type": "string",
        "minLength": 1,
        "maxLength": 8000
      },
      "expectedOutput": {
        "type": "string",
        "maxLength": 2000
      },
      "acceptanceCriteria": {
        "type": "array",
        "maxItems": 8,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "minLength": 1,
          "maxLength": 1000
        }
      },
      "outputSchema": {
        "type": "object"
      },
      "launchDigest": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "schemaVersion",
          "contextMode",
          "templateId",
          "templateVersion",
          "modelProfile",
          "thinkingLevel",
          "toolProfileVersion",
          "toolAllowlistMode",
          "tools",
          "piSkillsEnabled",
          "codexSkillsEnabled",
          "workspaceAccess",
          "workspaceRootCount",
          "outputContract",
          "extensionRuntime"
        ],
        "properties": {
          "schemaVersion": {
            "type": "string",
            "const": "rag-ime.agent-subagent-launch-digest.v1"
          },
          "contextMode": {
            "type": "string",
            "enum": [
              "fresh",
              "fork"
            ]
          },
          "templateId": {
            "type": "string"
          },
          "templateVersion": {
            "type": "string"
          },
          "modelProfile": {
            "type": "string"
          },
          "thinkingLevel": {
            "type": "string"
          },
          "toolProfileVersion": {
            "type": "string"
          },
          "toolAllowlistMode": {
            "type": "string",
            "enum": [
              "profile",
              "explicit"
            ]
          },
          "tools": {
            "type": "array",
            "uniqueItems": true,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 128
            }
          },
          "piSkillsEnabled": {
            "type": "boolean"
          },
          "codexSkillsEnabled": {
            "type": "boolean"
          },
          "workspaceAccess": {
            "type": "string",
            "enum": [
              "none",
              "read_only",
              "write"
            ]
          },
          "workspaceRootCount": {
            "type": "integer",
            "minimum": 0,
            "maximum": 4
          },
          "outputContract": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "required",
              "schemaSha256"
            ],
            "properties": {
              "required": {
                "type": "boolean"
              },
              "schemaSha256": {
                "type": "string",
                "pattern": "^$|^[a-f0-9]{64}$"
              }
            }
          },
          "extensionRuntime": {
            "type": "string",
            "const": "pi_host_managed"
          }
        }
      },
      "contract": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "status",
          "error",
          "toolCallId",
          "validatedAtMs"
        ],
        "properties": {
          "status": {
            "type": "string",
            "enum": [
              "not_requested",
              "pending",
              "valid",
              "invalid"
            ]
          },
          "error": {
            "type": "string",
            "maxLength": 500
          },
          "toolCallId": {
            "type": "string"
          },
          "validatedAtMs": {
            "type": [
              "integer",
              "null"
            ],
            "minimum": 0
          }
        }
      },
      "structuredOutput": {},
      "state": {
        "type": "string",
        "enum": [
          "queued",
          "running",
          "completed",
          "failed",
          "aborted",
          "timed_out"
        ]
      },
      "budget": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "maxTurns",
          "maxToolCalls",
          "maxTotalTokens",
          "maxDurationMs",
          "maxOutputChars"
        ],
        "properties": {
          "maxTurns": {
            "type": "integer",
            "minimum": 0,
            "maximum": 32
          },
          "maxToolCalls": {
            "type": "integer",
            "minimum": 0,
            "maximum": 64
          },
          "maxTotalTokens": {
            "type": "integer",
            "minimum": 256,
            "maximum": 262144
          },
          "maxDurationMs": {
            "type": "integer",
            "minimum": 1000,
            "maximum": 900000
          },
          "maxOutputChars": {
            "type": "integer",
            "minimum": 256,
            "maximum": 100000
          }
        }
      },
      "usage": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "turnCount",
          "toolCount",
          "totalTokens"
        ],
        "properties": {
          "turnCount": {
            "type": "integer",
            "minimum": 0
          },
          "toolCount": {
            "type": "integer",
            "minimum": 0
          },
          "totalTokens": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "result": {
        "type": "object"
      },
      "error": {
        "type": "string",
        "maxLength": 500
      },
      "artifact": {
        "type": "object",
        "required": [
          "schemaVersion",
          "artifactId",
          "ownerKind",
          "ownerId",
          "kind",
          "sha256"
        ],
        "properties": {
          "schemaVersion": {
            "type": "string",
            "const": "rag-ime.agent-artifact-ref.v1"
          },
          "artifactId": {
            "type": "string",
            "minLength": 1
          },
          "ownerKind": {
            "type": "string",
            "const": "subagent_run"
          },
          "ownerId": {
            "type": "string",
            "minLength": 1
          },
          "kind": {
            "type": "string",
            "const": "lifecycle"
          },
          "sha256": {
            "type": "string"
          }
        }
      },
      "supervision": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "phase",
          "reason",
          "requestedAtMs",
          "graceMs"
        ],
        "properties": {
          "phase": {
            "type": "string",
            "enum": [
              "none",
              "soft",
              "hard",
              "forced"
            ]
          },
          "reason": {
            "type": "string",
            "maxLength": 240
          },
          "requestedAtMs": {
            "type": [
              "integer",
              "null"
            ],
            "minimum": 0
          },
          "graceMs": {
            "type": "integer",
            "minimum": 0,
            "maximum": 30000
          }
        }
      },
      "resultContextScheduledAtMs": {
        "type": [
          "integer",
          "null"
        ],
        "minimum": 0
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "startedAtMs": {
        "type": [
          "integer",
          "null"
        ],
        "minimum": 0
      },
      "updatedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "completedAtMs": {
        "type": [
          "integer",
          "null"
        ],
        "minimum": 0
      }
    }
  },
  "agent-template.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-template.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "templateId",
      "version",
      "displayName",
      "summary",
      "contextModes",
      "toolProfileVersion",
      "defaultAccess",
      "allowedAccess",
      "budget",
      "capabilities"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-template.v1"
      },
      "templateId": {
        "type": "string",
        "enum": [
          "researcher",
          "planner",
          "worker",
          "reviewer",
          "delegate"
        ]
      },
      "version": {
        "type": "string",
        "const": "1"
      },
      "displayName": {
        "type": "string",
        "minLength": 1,
        "maxLength": 40
      },
      "summary": {
        "type": "string",
        "minLength": 1,
        "maxLength": 180
      },
      "contextModes": {
        "type": "array",
        "minItems": 1,
        "maxItems": 2,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "enum": [
            "fresh",
            "fork"
          ]
        }
      },
      "toolProfileVersion": {
        "type": "string",
        "enum": [
          "subagent-readonly-v1",
          "subagent-worker-v1"
        ]
      },
      "defaultAccess": {
        "type": "string",
        "enum": [
          "read_only",
          "write"
        ]
      },
      "allowedAccess": {
        "type": "array",
        "minItems": 1,
        "maxItems": 2,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "enum": [
            "read_only",
            "write"
          ]
        }
      },
      "budget": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "maxDepth",
          "maxTurns",
          "maxToolCalls",
          "maxTotalTokens",
          "maxDurationMs",
          "maxOutputChars"
        ],
        "properties": {
          "maxDepth": {
            "type": "integer",
            "minimum": 1,
            "maximum": 2
          },
          "maxTurns": {
            "type": "integer",
            "minimum": 0,
            "maximum": 32
          },
          "maxToolCalls": {
            "type": "integer",
            "minimum": 0,
            "maximum": 64
          },
          "maxTotalTokens": {
            "type": "integer",
            "minimum": 256,
            "maximum": 262144
          },
          "maxDurationMs": {
            "type": "integer",
            "minimum": 1000,
            "maximum": 900000
          },
          "maxOutputChars": {
            "type": "integer",
            "minimum": 256,
            "maximum": 100000
          }
        }
      },
      "capabilities": {
        "type": "array",
        "minItems": 1,
        "maxItems": 8,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "enum": [
            "rag",
            "memory",
            "planning",
            "review",
            "control",
            "delegation"
          ]
        }
      }
    }
  },
  "agent-thinking-selection.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-thinking-selection.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "ok",
      "sessionId",
      "thinkingLevel",
      "selected"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-thinking-selection.v1"
      },
      "ok": {
        "type": "boolean",
        "const": true
      },
      "sessionId": {
        "type": "string",
        "minLength": 1
      },
      "thinkingLevel": {
        "type": "string",
        "enum": [
          "off",
          "minimal",
          "low",
          "medium",
          "high",
          "xhigh",
          "max"
        ]
      },
      "selected": {
        "type": [
          "object",
          "null"
        ]
      }
    }
  },
  "agent-tool-call.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-tool-call.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "sessionId",
      "tool",
      "toolCallId",
      "args"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-tool-call.v1"
      },
      "sessionId": {
        "type": "string",
        "minLength": 1
      },
      "tool": {
        "type": "string",
        "enum": [
          "overview",
          "input",
          "voice",
          "planning",
          "agent_schedule",
          "memory",
          "agent_role_book",
          "knowledge",
          "models",
          "runtime",
          "configuration",
          "agents",
          "session_search",
          "trace_diagnostics",
          "room_partner",
          "structured_output",
          "browser",
          "todo",
          "agent_goal",
          "plugins",
          "sandbox",
          "work_documents",
          "lab_project",
          "lab_research",
          "desktop_semantic",
          "ls",
          "read",
          "grep",
          "find",
          "edit",
          "write",
          "bash",
          "workspace_list",
          "workspace_lsp",
          "workspace_read",
          "workspace_search",
          "workspace_patch",
          "workspace_edit",
          "workspace_write",
          "workspace_shell",
          "workspace_job"
        ]
      },
      "toolCallId": {
        "type": "string",
        "minLength": 1
      },
      "sourceLoopId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "args": {
        "type": "object"
      },
      "roomCapability": {
        "type": "object"
      },
      "loadReceiptId": {
        "type": "string"
      },
      "runtimeContext": {
        "type": "object",
        "required": [
          "schemaVersion",
          "forkSessions"
        ],
        "properties": {
          "schemaVersion": {
            "type": "string",
            "const": "rag-ime.agent-runtime-context.v1"
          },
          "forkSessions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 2,
            "items": {
              "type": "object",
              "required": [
                "sessionId",
                "sessionFile",
                "parentSessionFile",
                "parentLeafId"
              ],
              "properties": {
                "sessionId": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 128
                },
                "sessionFile": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 4096
                },
                "parentSessionFile": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 4096
                },
                "parentLeafId": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 128
                },
                "thinkingOverride": {
                  "type": "string",
                  "enum": [
                    "off"
                  ]
                }
              },
              "additionalProperties": false
            }
          }
        },
        "additionalProperties": false
      }
    },
    "additionalProperties": false
  },
  "agent-tool-result.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-tool-result.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "ok",
      "tool",
      "operation",
      "result"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.agent-tool-result.v1"
      },
      "ok": {
        "type": "boolean"
      },
      "tool": {
        "type": "string",
        "enum": [
          "overview",
          "input",
          "voice",
          "planning",
          "agent_schedule",
          "memory",
          "agent_role_book",
          "knowledge",
          "models",
          "runtime",
          "configuration",
          "agents",
          "session_search",
          "trace_diagnostics",
          "browser",
          "todo",
          "agent_goal",
          "desktop_semantic",
          "plugins",
          "sandbox",
          "work_documents",
          "lab_project",
          "lab_research",
          "workspace_list",
          "workspace_lsp",
          "workspace_read",
          "workspace_search",
          "workspace_patch",
          "workspace_edit",
          "workspace_write",
          "workspace_shell",
          "workspace_job"
        ]
      },
      "operation": {
        "type": "string",
        "enum": [
          "status",
          "capabilities",
          "recent_activity",
          "get_settings",
          "preview_settings",
          "apply_settings",
          "rollback_settings",
          "profile",
          "candidate_explain",
          "lexicon_review",
          "lexicon_apply",
          "lexicon_rollback",
          "privacy_policy",
          "provider_status",
          "provider_preview",
          "provider_apply",
          "provider_rollback",
          "dashboard",
          "task_action",
          "undo_task_event",
          "schedule",
          "runs",
          "pause",
          "resume",
          "cancel",
          "retry",
          "catalog",
          "read",
          "command",
          "execution_read",
          "execution_command",
          "knowledge_read",
          "knowledge_command",
          "app_command",
          "discover",
          "recent",
          "trace",
          "capture",
          "maintenance_status",
          "curation_prepare",
          "maintenance_preview",
          "maintenance_review",
          "maintenance_apply",
          "maintenance_rollback",
          "remember_preview",
          "correct_preview",
          "forget_preview",
          "remember_apply",
          "correct_apply",
          "forget_apply",
          "governance_rollback",
          "get",
          "explain",
          "history",
          "propose_revision",
          "review",
          "list",
          "history.search",
          "register",
          "archive",
          "repair",
          "reopen",
          "erase.preview",
          "erase",
          "inspect",
          "act",
          "update",
          "submit_review",
          "confirm_setup",
          "complete",
          "create_package",
          "validate",
          "propose_install",
          "propose_enable",
          "propose_disable",
          "propose_update",
          "propose_rollback",
          "propose_uninstall",
          "list_bases",
          "get_base",
          "list_documents",
          "create_base",
          "configure_base",
          "import_text",
          "rebuild_preview",
          "rebuild",
          "search",
          "find",
          "open",
          "recall",
          "deep_recall",
          "route_status",
          "profiles",
          "profile_preview",
          "profile_apply",
          "profile_rollback",
          "probe",
          "cache_stats",
          "health",
          "components",
          "diagnose",
          "pause_ai",
          "resume_ai",
          "restart_sidecar",
          "restart_predictor",
          "redeploy_rime",
          "audit",
          "export_preview",
          "export",
          "restore_preview",
          "restore_apply",
          "delegate",
          "call",
          "artifact",
          "abort",
          "tabs",
          "snapshot",
          "screenshot",
          "navigate",
          "click",
          "type",
          "scroll",
          "wait",
          "stop",
          "run",
          "apply",
          "start",
          "logs",
          "symbols",
          "hover",
          "definition",
          "references",
          "diagnostics",
          "rename",
          "code_action_apply",
          "init",
          "done",
          "drop",
          "append",
          "view",
          "rm"
        ]
      },
      "result": {
        "type": "object"
      }
    }
  },
  "agent-workflow-state.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.agent-workflow-state.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "ok",
      "sessionId",
      "todo",
      "goal",
      "actGate"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.agent-workflow-state.v1"
      },
      "ok": {
        "const": true
      },
      "sessionId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "todo": {
        "$ref": "#/$defs/todo"
      },
      "goal": {
        "$ref": "#/$defs/goal"
      },
      "actGate": {
        "$ref": "#/$defs/actGate"
      }
    },
    "$defs": {
      "todoTask": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "content",
          "status"
        ],
        "properties": {
          "content": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "status": {
            "type": "string",
            "enum": [
              "pending",
              "in_progress",
              "blocked",
              "completed",
              "abandoned"
            ]
          },
          "reason": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500
          }
        }
      },
      "todoPhase": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "name",
          "tasks"
        ],
        "properties": {
          "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80
          },
          "tasks": {
            "type": "array",
            "maxItems": 100,
            "items": {
              "$ref": "#/$defs/todoTask"
            }
          }
        }
      },
      "todo": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "schemaVersion",
          "id",
          "sessionId",
          "revision",
          "actor",
          "updatedAtMs",
          "roomLineage",
          "phases",
          "counts"
        ],
        "properties": {
          "schemaVersion": {
            "const": "rag-ime.agent-todo.v1"
          },
          "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 300
          },
          "sessionId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "revision": {
            "type": "integer",
            "minimum": 0
          },
          "actor": {
            "type": "string",
            "maxLength": 120
          },
          "updatedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "roomLineage": {
            "oneOf": [
              {
                "$ref": "#/$defs/roomTodoLineage"
              },
              {
                "type": "null"
              }
            ]
          },
          "phases": {
            "type": "array",
            "maxItems": 16,
            "items": {
              "$ref": "#/$defs/todoPhase"
            }
          },
          "counts": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "total",
              "pending",
              "inProgress",
              "completed",
              "abandoned"
            ],
            "properties": {
              "total": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100
              },
              "pending": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100
              },
              "inProgress": {
                "type": "integer",
                "minimum": 0,
                "maximum": 1
              },
              "blocked": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100
              },
              "completed": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100
              },
              "abandoned": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100
              }
            }
          }
        }
      },
      "roomTodoLineage": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "schemaVersion",
          "roomId",
          "rootId",
          "taskId",
          "workItemId",
          "dispatchId",
          "sessionId",
          "participantId",
          "generation",
          "taskRevision",
          "ownershipRevision",
          "workItemRevision"
        ],
        "properties": {
          "schemaVersion": {
            "const": "wisdom-weasel.room-todo-lineage.v1"
          },
          "roomId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 320
          },
          "rootId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 320
          },
          "taskId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 320
          },
          "workItemId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 320
          },
          "dispatchId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 320
          },
          "sessionId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "participantId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 320
          },
          "generation": {
            "type": "integer",
            "minimum": 0
          },
          "taskRevision": {
            "type": "integer",
            "minimum": 0
          },
          "ownershipRevision": {
            "type": "integer",
            "minimum": 0
          },
          "workItemRevision": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "evidence": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "kind",
          "summary",
          "reference"
        ],
        "properties": {
          "kind": {
            "type": "string",
            "enum": [
              "test",
              "artifact",
              "commit",
              "receipt",
              "note"
            ]
          },
          "summary": {
            "type": "string",
            "minLength": 1,
            "maxLength": 600
          },
          "reference": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000
          }
        }
      },
      "completionAudit": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "auditId",
          "summary",
          "evidence",
          "completedBy",
          "createdAtMs"
        ],
        "properties": {
          "auditId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "summary": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2000
          },
          "evidence": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": {
              "$ref": "#/$defs/evidence"
            }
          },
          "completedBy": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "cancellationAudit": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "auditId",
          "reason",
          "cancelledBy",
          "createdAtMs"
        ],
        "properties": {
          "auditId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "reason": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000
          },
          "cancelledBy": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "goal": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "schemaVersion",
          "sessionId",
          "configured",
          "goalId",
          "revision",
          "objective",
          "successCriteria",
          "evidenceExpectations",
          "status",
          "budget",
          "usage",
          "remaining",
          "budgetExceeded",
          "completionAudit",
          "cancellationAudit",
          "updatedAtMs"
        ],
        "properties": {
          "schemaVersion": {
            "const": "rag-ime.agent-goal.v1"
          },
          "sessionId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "configured": {
            "type": "boolean"
          },
          "goalId": {
            "type": "string",
            "maxLength": 240
          },
          "revision": {
            "type": "integer",
            "minimum": 0
          },
          "objective": {
            "type": "string",
            "maxLength": 4000
          },
          "successCriteria": {
            "type": "string",
            "maxLength": 2000
          },
          "evidenceExpectations": {
            "type": "array",
            "maxItems": 20,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 600
            }
          },
          "status": {
            "type": "string",
            "enum": [
              "active",
              "paused",
              "completed",
              "cancelled",
              "cleared"
            ]
          },
          "budget": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "tokenLimit",
              "timeLimitMs"
            ],
            "properties": {
              "tokenLimit": {
                "type": [
                  "integer",
                  "null"
                ],
                "minimum": 1
              },
              "timeLimitMs": {
                "type": [
                  "integer",
                  "null"
                ],
                "minimum": 1
              }
            }
          },
          "usage": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "tokens",
              "elapsedMs"
            ],
            "properties": {
              "tokens": {
                "type": "integer",
                "minimum": 0
              },
              "elapsedMs": {
                "type": "integer",
                "minimum": 0
              }
            }
          },
          "remaining": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "tokens",
              "timeMs"
            ],
            "properties": {
              "tokens": {
                "type": [
                  "integer",
                  "null"
                ],
                "minimum": 0
              },
              "timeMs": {
                "type": [
                  "integer",
                  "null"
                ],
                "minimum": 0
              }
            }
          },
          "budgetExceeded": {
            "type": "boolean"
          },
          "completionAudit": {
            "oneOf": [
              {
                "$ref": "#/$defs/completionAudit"
              },
              {
                "type": "null"
              }
            ]
          },
          "cancellationAudit": {
            "oneOf": [
              {
                "$ref": "#/$defs/cancellationAudit"
              },
              {
                "type": "null"
              }
            ]
          },
          "updatedAtMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "actGate": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "allowed",
          "reason",
          "message",
          "todoRevision",
          "goalRevision"
        ],
        "properties": {
          "allowed": {
            "type": "boolean"
          },
          "reason": {
            "type": "string",
            "enum": [
              "approved",
              "user_execution_request",
              "goal_paused",
              "goal_completed",
              "goal_cancelled",
              "goal_budget_exhausted"
            ]
          },
          "message": {
            "type": "string",
            "minLength": 1,
            "maxLength": 300
          },
          "todoRevision": {
            "type": "integer",
            "minimum": 0
          },
          "goalRevision": {
            "type": "integer",
            "minimum": 0
          }
        }
      }
    }
  },
  "assistant-candidate-action.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.assistant-candidate-action.v1",
    "type": "object",
    "required": [
      "action",
      "candidate"
    ],
    "properties": {
      "action": {
        "type": "string",
        "enum": [
          "remember",
          "suppress"
        ]
      },
      "candidate": {
        "type": "object",
        "properties": {
          "text": {
            "type": "string"
          },
          "insertText": {
            "type": "string"
          },
          "sourceType": {
            "type": "string"
          },
          "memoryId": {
            "type": "string"
          },
          "sourceEventId": {
            "type": [
              "integer",
              "null"
            ]
          },
          "suggestionId": {
            "type": "string"
          },
          "candidateStableId": {
            "type": "string"
          }
        },
        "additionalProperties": true
      },
      "query": {
        "type": "string"
      },
      "project": {
        "type": "string"
      },
      "app": {
        "type": "string"
      },
      "frontAppBundleId": {
        "type": "string"
      },
      "frontmostApp": {
        "type": "string"
      },
      "bundleId": {
        "type": "string"
      },
      "privacyDisposition": {
        "type": "string",
        "enum": [
          "allowed",
          "sensitive",
          "unknown"
        ]
      },
      "sensitiveField": {
        "type": "boolean"
      },
      "secureInput": {
        "type": "boolean"
      }
    },
    "additionalProperties": false
  },
  "assistant-overlay.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.assistant-overlay.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "visible",
      "uiMode",
      "phase",
      "inputMode",
      "candidates",
      "keyPolicy",
      "frontendTransaction"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.assistant-overlay.v1"
      },
      "visible": {
        "type": "boolean"
      },
      "uiMode": {
        "type": "string"
      },
      "phase": {
        "type": "string"
      },
      "inputMode": {
        "type": "string"
      },
      "statusText": {
        "type": "string"
      },
      "snapshotId": {
        "type": "string"
      },
      "sessionFingerprint": {
        "type": "string"
      },
      "expiresAfterMs": {
        "type": "integer",
        "minimum": 0
      },
      "candidates": {
        "type": "array",
        "items": {
          "$ref": "#/$defs/candidate"
        }
      },
      "sourceCards": {
        "type": "array"
      },
      "keyPolicy": {
        "type": "object"
      },
      "overlayConfig": {
        "type": "object"
      },
      "progressive": {
        "type": "object"
      },
      "frontendTransaction": {
        "type": "object"
      },
      "dismissReason": {
        "type": "string"
      }
    },
    "$defs": {
      "candidate": {
        "type": "object",
        "required": [
          "insertText",
          "sourceType"
        ],
        "properties": {
          "text": {
            "type": "string"
          },
          "insertText": {
            "type": "string",
            "minLength": 1
          },
          "sourceType": {
            "type": "string",
            "enum": [
              "model",
              "rag",
              "memory",
              "action"
            ]
          },
          "candidateStableId": {
            "type": "string"
          },
          "snapshotId": {
            "type": "string"
          },
          "selectionAction": {
            "type": "string"
          },
          "sourceBadge": {
            "type": "string"
          },
          "memoryId": {
            "type": "string"
          },
          "suggestionId": {
            "type": "string"
          },
          "sourceEventId": {
            "type": [
              "integer",
              "null"
            ]
          },
          "metadata": {
            "type": "object"
          }
        }
      }
    }
  },
  "capability-catalog.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.capability-catalog.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "ok",
      "revision",
      "effectiveAtMs",
      "projectScope",
      "items"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.capability-catalog.v1"
      },
      "ok": {
        "type": "boolean",
        "const": true
      },
      "revision": {
        "type": "string",
        "pattern": "^sha256:[a-f0-9]{64}$"
      },
      "effectiveAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "projectScope": {
        "oneOf": [
          {
            "type": "object",
            "required": [
              "supported",
              "identityKind",
              "projectId",
              "reason"
            ],
            "properties": {
              "supported": {
                "type": "boolean",
                "const": true
              },
              "identityKind": {
                "type": "string",
                "const": "workspace_scope_sha256"
              },
              "projectId": {
                "type": "string",
                "pattern": "^workspace-[a-f0-9]{64}$"
              },
              "reason": {
                "type": "string",
                "const": "session_workspace_scope"
              }
            },
            "additionalProperties": false
          },
          {
            "type": "object",
            "required": [
              "supported",
              "identityKind",
              "reason"
            ],
            "properties": {
              "supported": {
                "type": "boolean",
                "const": false
              },
              "identityKind": {
                "type": "string",
                "const": "none"
              },
              "reason": {
                "type": "string",
                "const": "stable_project_identity_unavailable"
              }
            },
            "additionalProperties": false
          }
        ]
      },
      "sessionPolicy": {
        "type": "object",
        "required": [
          "sessionId",
          "mode",
          "executionMode",
          "workspaceScopeGranted",
          "toolProfileVersion",
          "toolAllowlistMode",
          "allowedTools",
          "policyRevision",
          "disclosurePreferences",
          "effectiveAtMs"
        ],
        "properties": {
          "sessionId": {
            "type": "string",
            "minLength": 1
          },
          "mode": {
            "type": "string",
            "enum": [
              "assistant",
              "coordinator"
            ]
          },
          "executionMode": {
            "type": "string",
            "enum": [
              "read_only",
              "per_action",
              "workspace_managed",
              "full_trust"
            ]
          },
          "workspaceScopeGranted": {
            "type": "boolean"
          },
          "toolProfileVersion": {
            "type": "string",
            "minLength": 1
          },
          "toolAllowlistMode": {
            "type": "string",
            "enum": [
              "profile",
              "explicit"
            ]
          },
          "allowedTools": {
            "type": "array",
            "items": {
              "type": "string"
            }
          },
          "policyRevision": {
            "type": "integer",
            "minimum": 1
          },
          "effectiveAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "disclosurePreferences": {
            "type": "object",
            "required": [
              "globalDefault",
              "projectDefault",
              "session",
              "effective"
            ],
            "properties": {
              "globalDefault": {
                "$ref": "#/$defs/preferences"
              },
              "projectDefault": {
                "$ref": "#/$defs/preferences"
              },
              "session": {
                "$ref": "#/$defs/preferences"
              },
              "effective": {
                "type": "object",
                "additionalProperties": {
                  "type": "string",
                  "enum": [
                    "enabled",
                    "disabled"
                  ]
                }
              }
            },
            "additionalProperties": false
          }
        },
        "additionalProperties": false
      },
      "items": {
        "type": "array",
        "items": {
          "type": "object",
          "required": [
            "id",
            "canonicalId",
            "kind",
            "source",
            "status",
            "risk",
            "requiredPermissions",
            "authorization",
            "disclosure",
            "effectiveScope",
            "reasons",
            "revision",
            "effectiveAtMs"
          ],
          "properties": {
            "id": {
              "type": "string",
              "minLength": 1
            },
            "canonicalId": {
              "type": "string",
              "pattern": "^(tool|skill|extension):.+$"
            },
            "kind": {
              "type": "string",
              "enum": [
                "tool",
                "skill",
                "extension"
              ]
            },
            "source": {
              "type": "object",
              "required": [
                "kind",
                "label"
              ],
              "properties": {
                "kind": {
                  "type": "string",
                  "minLength": 1
                },
                "label": {
                  "type": "string",
                  "minLength": 1
                }
              }
            },
            "status": {
              "type": "string",
              "minLength": 1
            },
            "risk": {
              "type": "string",
              "minLength": 1
            },
            "requiredPermissions": {
              "type": "array",
              "items": {
                "type": "string"
              },
              "uniqueItems": true
            },
            "authorization": {
              "type": "object",
              "required": [
                "state",
                "reason"
              ],
              "properties": {
                "state": {
                  "type": "string",
                  "enum": [
                    "authorized",
                    "denied",
                    "not_applicable"
                  ]
                },
                "reason": {
                  "type": "string",
                  "minLength": 1
                }
              },
              "additionalProperties": false
            },
            "disclosure": {
              "type": "object",
              "required": [
                "preference",
                "effective",
                "state",
                "reason",
                "scope"
              ],
              "properties": {
                "preference": {
                  "type": "string",
                  "enum": [
                    "inherit",
                    "enabled",
                    "disabled"
                  ]
                },
                "effective": {
                  "type": "string",
                  "enum": [
                    "enabled",
                    "disabled"
                  ]
                },
                "state": {
                  "type": "string",
                  "enum": [
                    "disclosed",
                    "hidden"
                  ]
                },
                "reason": {
                  "type": "string",
                  "minLength": 1
                },
                "scope": {
                  "type": "string",
                  "enum": [
                    "session",
                    "project_default",
                    "global_default",
                    "built_in_default"
                  ]
                }
              },
              "additionalProperties": false
            },
            "effectiveScope": {
              "type": "string",
              "enum": [
                "session",
                "project_default",
                "global_default",
                "built_in_default"
              ]
            },
            "reasons": {
              "type": "array",
              "items": {
                "type": "string",
                "minLength": 1
              }
            },
            "revision": {
              "type": "string",
              "minLength": 1
            },
            "effectiveAtMs": {
              "type": "integer",
              "minimum": 0
            }
          }
        }
      }
    },
    "$defs": {
      "preferences": {
        "type": "object",
        "additionalProperties": {
          "type": "string",
          "enum": [
            "inherit",
            "enabled",
            "disabled"
          ]
        }
      }
    }
  },
  "collaboration-profile-command-receipt.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.collaboration-profile-command-receipt.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "receiptId",
      "commandId",
      "commandHash",
      "action",
      "status",
      "profileId",
      "routeHash",
      "guardEpoch",
      "result",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.collaboration-profile-command-receipt.v1"
      },
      "receiptId": {
        "type": "string",
        "pattern": "^profile-command-receipt:[a-f0-9]{24}$"
      },
      "commandId": {
        "type": "string",
        "pattern": "^profile-command:[A-Za-z0-9._:-]{1,96}$"
      },
      "commandHash": {
        "type": "string",
        "pattern": "^sha256:[a-f0-9]{64}$"
      },
      "action": {
        "type": "string",
        "enum": [
          "inspect",
          "validate",
          "compile",
          "dry_run",
          "stage",
          "activate",
          "rollback",
          "revoke"
        ]
      },
      "status": {
        "type": "string",
        "const": "applied"
      },
      "profileId": {
        "type": [
          "string",
          "null"
        ]
      },
      "routeHash": {
        "type": "string",
        "pattern": "^sha256:[a-f0-9]{64}$"
      },
      "guardEpoch": {
        "type": "integer",
        "minimum": 0
      },
      "result": {
        "type": "object"
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      }
    }
  },
  "collaboration-profile-command.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.collaboration-profile-command.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "commandId",
      "action",
      "idempotencyKey",
      "actorRef",
      "payload",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.collaboration-profile-command.v1"
      },
      "commandId": {
        "type": "string",
        "pattern": "^profile-command:[A-Za-z0-9._:-]{1,96}$"
      },
      "action": {
        "type": "string",
        "enum": [
          "inspect",
          "validate",
          "compile",
          "dry_run",
          "stage",
          "activate",
          "rollback",
          "revoke"
        ]
      },
      "idempotencyKey": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "actorRef": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "profileId": {
        "type": "string",
        "pattern": "^[a-z0-9][a-z0-9-]{1,62}$"
      },
      "candidateId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "contentHash": {
        "type": "string",
        "pattern": "^sha256:[a-f0-9]{64}$"
      },
      "expectedPointerRevision": {
        "type": "integer",
        "minimum": 0
      },
      "activationScope": {
        "type": "string",
        "enum": [
          "immediate",
          "new_roots_only"
        ]
      },
      "adminConfirmation": {
        "type": "string",
        "maxLength": 80
      },
      "payload": {
        "type": "object"
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      }
    }
  },
  "collaboration-profile-compile-receipt.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.collaboration-profile-compile-receipt.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "receiptId",
      "contentHash",
      "compilerVersion",
      "bindingRevision",
      "baselineCapabilities",
      "requestedCapabilities",
      "effectiveCapabilities",
      "rejectedCapabilities",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.collaboration-profile-compile-receipt.v1"
      },
      "receiptId": {
        "type": "string",
        "pattern": "^profile-compile:[a-f0-9]{24}$"
      },
      "contentHash": {
        "type": "string",
        "pattern": "^sha256:[a-f0-9]{64}$"
      },
      "compilerVersion": {
        "type": "string",
        "const": "collaboration-profile-compiler-v1"
      },
      "bindingRevision": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "baselineCapabilities": {
        "$ref": "#/$defs/capabilities"
      },
      "requestedCapabilities": {
        "$ref": "#/$defs/capabilities"
      },
      "effectiveCapabilities": {
        "$ref": "#/$defs/capabilities"
      },
      "rejectedCapabilities": {
        "$ref": "#/$defs/capabilities"
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      }
    },
    "$defs": {
      "capabilities": {
        "type": "array",
        "maxItems": 8,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "enum": [
            "rag",
            "memory",
            "planning",
            "review",
            "control",
            "delegation"
          ]
        }
      }
    }
  },
  "collaboration-profile-projection.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.collaboration-profile-projection.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "profileId",
      "routeHash",
      "requiredReadScopes",
      "requiredWriteScopes",
      "guardEpoch",
      "normalAgentFallback",
      "inspection",
      "recentReceipts"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.collaboration-profile-projection.v1"
      },
      "profileId": {
        "type": "string",
        "pattern": "^[a-z0-9][a-z0-9-]{1,62}$"
      },
      "routeHash": {
        "type": "string",
        "pattern": "^sha256:[a-f0-9]{64}$"
      },
      "requiredReadScopes": {
        "type": "array",
        "minItems": 1,
        "maxItems": 1,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "enum": [
            "agent.read"
          ]
        }
      },
      "requiredWriteScopes": {
        "type": "array",
        "minItems": 2,
        "maxItems": 2,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "enum": [
            "agent.write",
            "agent.approve"
          ]
        }
      },
      "guardEpoch": {
        "type": "integer",
        "minimum": 0
      },
      "normalAgentFallback": {
        "type": "boolean",
        "const": true
      },
      "inspection": {
        "type": "object"
      },
      "recentReceipts": {
        "type": "array",
        "maxItems": 50,
        "items": {
          "type": "object"
        }
      }
    }
  },
  "collaboration-profile.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.collaboration-profile.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "profileId",
      "version",
      "displayName",
      "summary",
      "collaborationRoleRefs",
      "capabilityRequests",
      "requiredGateIds",
      "promptGuidance",
      "trustTier"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.collaboration-profile.v1"
      },
      "profileId": {
        "type": "string",
        "pattern": "^[a-z0-9][a-z0-9-]{1,62}$"
      },
      "version": {
        "type": "string",
        "pattern": "^[1-9][0-9]{0,5}$"
      },
      "displayName": {
        "type": "string",
        "minLength": 1,
        "maxLength": 80
      },
      "summary": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "collaborationRoleRefs": {
        "type": "array",
        "minItems": 1,
        "maxItems": 16,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9-]{1,62}@[1-9][0-9]{0,5}$"
        }
      },
      "capabilityRequests": {
        "type": "array",
        "minItems": 1,
        "maxItems": 8,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "enum": [
            "rag",
            "memory",
            "planning",
            "review",
            "control",
            "delegation"
          ]
        }
      },
      "requiredGateIds": {
        "type": "array",
        "minItems": 1,
        "maxItems": 16,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9-]{1,80}$"
        }
      },
      "promptGuidance": {
        "type": "array",
        "minItems": 1,
        "maxItems": 12,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "minLength": 1,
          "maxLength": 240
        }
      },
      "trustTier": {
        "type": "string",
        "enum": [
          "builtin",
          "signed",
          "local-untrusted"
        ]
      }
    }
  },
  "collaboration-role.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.collaboration-role.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "roleId",
      "version",
      "displayName",
      "summary",
      "responsibilities",
      "entryConditions",
      "exitConditions",
      "allowedCommitDecisions",
      "capabilityRestrictions"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.collaboration-role.v1"
      },
      "roleId": {
        "type": "string",
        "enum": [
          "coordinator",
          "researcher",
          "implementer",
          "reviewer",
          "specialist"
        ]
      },
      "version": {
        "type": "string",
        "const": "1"
      },
      "displayName": {
        "type": "string",
        "minLength": 1,
        "maxLength": 40
      },
      "summary": {
        "type": "string",
        "minLength": 1,
        "maxLength": 180
      },
      "responsibilities": {
        "type": "array",
        "minItems": 1,
        "maxItems": 8,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "minLength": 1,
          "maxLength": 160
        }
      },
      "entryConditions": {
        "type": "array",
        "minItems": 1,
        "maxItems": 8,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "minLength": 1,
          "maxLength": 160
        }
      },
      "exitConditions": {
        "type": "array",
        "minItems": 1,
        "maxItems": 8,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "minLength": 1,
          "maxLength": 160
        }
      },
      "allowedCommitDecisions": {
        "type": "array",
        "minItems": 1,
        "maxItems": 4,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "enum": [
            "deliver",
            "handoff",
            "wait",
            "blocked"
          ]
        }
      },
      "capabilityRestrictions": {
        "type": "array",
        "minItems": 1,
        "maxItems": 8,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "enum": [
            "rag",
            "memory",
            "planning",
            "review",
            "control",
            "delegation"
          ]
        }
      }
    }
  },
  "compiled-agent-runtime-profile.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.compiled-agent-runtime-profile.v1",
    "$defs": {
      "definitionRef": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "kind",
          "id",
          "version",
          "contentHash"
        ],
        "properties": {
          "kind": {
            "type": "string",
            "enum": [
              "persona",
              "collaboration-role",
              "agent-template",
              "collaboration-profile"
            ]
          },
          "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80
          },
          "version": {
            "type": "string",
            "pattern": "^[1-9][0-9]{0,5}$"
          },
          "contentHash": {
            "type": "string",
            "pattern": "^sha256:[a-f0-9]{64}$"
          }
        }
      }
    },
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "compilerVersion",
      "personaRef",
      "collaborationRoleRef",
      "templateRef",
      "collaborationProfileRef",
      "effectiveCapabilities",
      "rejectedCapabilities",
      "capabilityRevision",
      "promptPlanRevision",
      "skillPolicyRevision",
      "contextPolicyRevision",
      "contentHash"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.compiled-agent-runtime-profile.v1"
      },
      "compilerVersion": {
        "type": "string",
        "const": "agent-definition-compiler-v1"
      },
      "personaRef": {
        "$ref": "#/$defs/definitionRef"
      },
      "collaborationRoleRef": {
        "$ref": "#/$defs/definitionRef"
      },
      "templateRef": {
        "$ref": "#/$defs/definitionRef"
      },
      "collaborationProfileRef": {
        "anyOf": [
          {
            "$ref": "#/$defs/definitionRef"
          },
          {
            "type": "null"
          }
        ]
      },
      "effectiveCapabilities": {
        "type": "array",
        "maxItems": 8,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "enum": [
            "rag",
            "memory",
            "planning",
            "review",
            "control",
            "delegation"
          ]
        }
      },
      "rejectedCapabilities": {
        "type": "array",
        "maxItems": 8,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "enum": [
            "rag",
            "memory",
            "planning",
            "review",
            "control",
            "delegation"
          ]
        }
      },
      "capabilityRevision": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "promptPlanRevision": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "skillPolicyRevision": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "contextPolicyRevision": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "contentHash": {
        "type": "string",
        "pattern": "^sha256:[a-f0-9]{64}$"
      }
    }
  },
  "control-tool-manifest.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.control-tool-manifest.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "id",
      "domain",
      "displayName",
      "description",
      "category",
      "riskLevel",
      "sessionModes",
      "operations",
      "resultPresentation",
      "availability",
      "version"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.control-tool-manifest.v1"
      },
      "id": {
        "type": "string",
        "minLength": 1
      },
      "domain": {
        "type": "string",
        "minLength": 1
      },
      "displayName": {
        "type": "string",
        "minLength": 1
      },
      "description": {
        "type": "string",
        "minLength": 1
      },
      "category": {
        "type": "string",
        "enum": [
          "overview",
          "input",
          "voice",
          "planning",
          "memory",
          "knowledge",
          "models",
          "runtime",
          "configuration",
          "agents",
          "browser",
          "desktop",
          "workspace"
        ]
      },
      "riskLevel": {
        "type": "string",
        "enum": [
          "R0",
          "R1",
          "R2",
          "R3"
        ]
      },
      "operationRisks": {
        "type": "object",
        "additionalProperties": {
          "type": "string",
          "enum": [
            "R0",
            "R1",
            "R2",
            "R3"
          ]
        }
      },
      "sessionModes": {
        "type": "array",
        "items": {
          "type": "string",
          "enum": [
            "assistant",
            "coordinator"
          ]
        }
      },
      "operations": {
        "type": "array",
        "items": {
          "type": "string"
        }
      },
      "resultPresentation": {
        "type": "string",
        "enum": [
          "status",
          "table",
          "citation",
          "tool_result",
          "diff",
          "approval",
          "terminal",
          "media"
        ]
      },
      "availability": {
        "type": "string",
        "enum": [
          "online",
          "offline",
          "disabled",
          "unconfigured"
        ]
      },
      "version": {
        "type": "string",
        "minLength": 1
      },
      "alwaysAvailable": {
        "type": "boolean"
      }
    }
  },
  "daily-activity-timeline.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.daily-activity-timeline.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "timelineId",
      "project",
      "date",
      "timezone",
      "status",
      "sourceEventIds",
      "sourceEventHash",
      "segments",
      "summary",
      "eventCount",
      "segmentCount",
      "observedStartMs",
      "observedEndMs",
      "spanSemantics",
      "ordinaryActivityCount",
      "consolidatedActivityCount",
      "approvedBookId",
      "approvedBy",
      "approvedAtMs",
      "createdAtMs",
      "updatedAtMs",
      "policy"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.daily-activity-timeline.v1"
      },
      "timelineId": {
        "type": "string",
        "minLength": 1
      },
      "project": {
        "type": "string"
      },
      "date": {
        "type": "string",
        "pattern": "^\\d{4}-\\d{2}-\\d{2}$"
      },
      "timezone": {
        "type": "string",
        "minLength": 1
      },
      "status": {
        "type": "string",
        "enum": [
          "draft",
          "approved",
          "rejected",
          "superseded"
        ]
      },
      "sourceEventIds": {
        "type": "array",
        "items": {
          "type": "integer",
          "minimum": 1
        },
        "uniqueItems": true
      },
      "sourceEventHash": {
        "type": "string",
        "pattern": "^[a-f0-9]{64}$"
      },
      "segments": {
        "type": "array",
        "items": {
          "$ref": "#/$defs/segment"
        }
      },
      "summary": {
        "type": "string",
        "minLength": 1
      },
      "eventCount": {
        "type": "integer",
        "minimum": 1
      },
      "segmentCount": {
        "type": "integer",
        "minimum": 1
      },
      "observedStartMs": {
        "type": "integer",
        "minimum": 0
      },
      "observedEndMs": {
        "type": "integer",
        "minimum": 0
      },
      "spanSemantics": {
        "type": "string",
        "const": "first_to_last_source_event"
      },
      "ordinaryActivityCount": {
        "type": "integer",
        "minimum": 0
      },
      "consolidatedActivityCount": {
        "type": "integer",
        "minimum": 0
      },
      "approvedBookId": {
        "type": "string"
      },
      "approvedBy": {
        "type": "string"
      },
      "approvedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "updatedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "segmentationMode": {
        "type": "string",
        "enum": [
          "semantic_task_v5",
          "semantic_task_v4",
          "semantic_task_v3",
          "semantic_task_v2",
          "legacy_app_interval_v1"
        ]
      },
      "source": {
        "$ref": "#/$defs/source"
      },
      "ref": {
        "$ref": "#/$defs/ref"
      },
      "policy": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "derivedFromInputEvents",
          "longTermFact",
          "automaticPromotion",
          "explicitApprovalRequired",
          "minimumConsolidatedSpanMs"
        ],
        "properties": {
          "derivedFromInputEvents": {
            "type": "boolean",
            "const": true
          },
          "longTermFact": {
            "type": "boolean",
            "const": false
          },
          "automaticPromotion": {
            "type": "boolean",
            "const": true
          },
          "explicitApprovalRequired": {
            "type": "boolean",
            "const": false
          },
          "minimumConsolidatedSpanMs": {
            "type": "integer",
            "minimum": 1
          }
        }
      }
    },
    "$defs": {
      "segment": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "segmentId",
          "position",
          "app",
          "sourceKinds",
          "contextGroupIds",
          "startMs",
          "endMs",
          "period",
          "eventCount",
          "sourceEventIds",
          "sourceEventHash",
          "summary",
          "redactedEventCount",
          "activityKind",
          "spanSemantics"
        ],
        "properties": {
          "segmentId": {
            "type": "string",
            "minLength": 1
          },
          "position": {
            "type": "integer",
            "minimum": 0
          },
          "app": {
            "type": "string",
            "minLength": 1
          },
          "sourceKinds": {
            "type": "array",
            "items": {
              "type": "string",
              "minLength": 1
            },
            "uniqueItems": true
          },
          "contextGroupIds": {
            "type": "array",
            "items": {
              "type": "string",
              "minLength": 1
            },
            "uniqueItems": true
          },
          "startMs": {
            "type": "integer",
            "minimum": 0
          },
          "endMs": {
            "type": "integer",
            "minimum": 0
          },
          "period": {
            "type": "string",
            "enum": [
              "day",
              "morning",
              "afternoon",
              "evening"
            ]
          },
          "eventCount": {
            "type": "integer",
            "minimum": 1
          },
          "sourceEventIds": {
            "type": "array",
            "items": {
              "type": "integer",
              "minimum": 1
            },
            "uniqueItems": true
          },
          "sourceEventHash": {
            "type": "string",
            "pattern": "^[a-f0-9]{64}$"
          },
          "summary": {
            "type": "string",
            "minLength": 1
          },
          "redactedEventCount": {
            "type": "integer",
            "minimum": 0
          },
          "activityKind": {
            "type": "string",
            "enum": [
              "ordinary_activity",
              "consolidated_activity"
            ]
          },
          "spanSemantics": {
            "type": "string",
            "const": "first_to_last_source_event"
          },
          "title": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "apps": {
            "type": "array",
            "items": {
              "type": "string",
              "minLength": 1
            },
            "uniqueItems": true
          },
          "evidenceRefs": {
            "type": "array",
            "items": {
              "$ref": "#/$defs/evidenceRef"
            }
          },
          "source": {
            "$ref": "#/$defs/source"
          },
          "ref": {
            "$ref": "#/$defs/ref"
          }
        }
      },
      "source": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "type",
          "id"
        ],
        "properties": {
          "type": {
            "type": "string"
          },
          "id": {
            "type": "string"
          }
        }
      },
      "ref": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "type",
          "id"
        ],
        "properties": {
          "type": {
            "type": "string"
          },
          "id": {
            "type": "string"
          }
        }
      },
      "evidenceRef": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "sourceType",
          "sourceId",
          "eventId",
          "app",
          "sourceKind",
          "occurredAtMs",
          "redacted"
        ],
        "properties": {
          "sourceType": {
            "type": "string",
            "const": "input_event"
          },
          "sourceId": {
            "type": "string",
            "minLength": 1
          },
          "eventId": {
            "type": "integer",
            "minimum": 1
          },
          "app": {
            "type": "string",
            "minLength": 1
          },
          "sourceKind": {
            "type": "string",
            "minLength": 1
          },
          "occurredAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "redacted": {
            "type": "boolean"
          }
        }
      }
    }
  },
  "daily-conversation-digest.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.daily-conversation-digest.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "digestId",
      "project",
      "roleId",
      "window",
      "sourceEvidenceIds",
      "activityTimelineId",
      "activityContext",
      "sourceCounts",
      "summary",
      "highlights",
      "recentWork",
      "caveats",
      "generatedAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.daily-conversation-digest.v1"
      },
      "digestId": {
        "type": "string",
        "minLength": 1
      },
      "project": {
        "type": "string"
      },
      "roleId": {
        "type": "string",
        "minLength": 1
      },
      "window": {
        "type": "object",
        "required": [
          "startMs",
          "endMs"
        ],
        "properties": {
          "startMs": {
            "type": "integer",
            "minimum": 0
          },
          "endMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "sourceEvidenceIds": {
        "type": "array",
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "activityTimelineId": {
        "type": "string"
      },
      "activityContext": {
        "$ref": "#/$defs/activityContext"
      },
      "sourceCounts": {
        "type": "object"
      },
      "summary": {
        "type": "string",
        "minLength": 1
      },
      "highlights": {
        "type": "array",
        "items": {
          "$ref": "#/$defs/digestItem"
        }
      },
      "recentWork": {
        "type": "array",
        "items": {
          "$ref": "#/$defs/digestItem"
        }
      },
      "caveats": {
        "type": "array",
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "generatedAtMs": {
        "type": "integer",
        "minimum": 0
      }
    },
    "$defs": {
      "activityContext": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "schemaVersion",
          "available",
          "date",
          "timelineId",
          "status",
          "sourceEventHash",
          "summary",
          "segments",
          "eventCount",
          "retainedEventCount",
          "filteredInternalEventCount",
          "deduplicatedEventCount",
          "redactedEventCount",
          "corroborationOnly",
          "maySupportFacts"
        ],
        "properties": {
          "schemaVersion": {
            "type": "string",
            "const": "rag-ime.activity-timeline-context.v1"
          },
          "available": {
            "type": "boolean"
          },
          "date": {
            "type": "string",
            "pattern": "^\\d{4}-\\d{2}-\\d{2}$"
          },
          "timelineId": {
            "type": "string"
          },
          "status": {
            "type": "string",
            "enum": [
              "unavailable",
              "draft",
              "approved"
            ]
          },
          "sourceEventHash": {
            "type": "string",
            "pattern": "^$|^[a-f0-9]{64}$"
          },
          "summary": {
            "type": "string",
            "maxLength": 2400
          },
          "segments": {
            "type": "array",
            "maxItems": 12,
            "items": {
              "type": "object"
            }
          },
          "eventCount": {
            "type": "integer",
            "minimum": 0
          },
          "retainedEventCount": {
            "type": "integer",
            "minimum": 0
          },
          "filteredInternalEventCount": {
            "type": "integer",
            "minimum": 0
          },
          "deduplicatedEventCount": {
            "type": "integer",
            "minimum": 0
          },
          "redactedEventCount": {
            "type": "integer",
            "minimum": 0
          },
          "corroborationOnly": {
            "type": "boolean",
            "const": true
          },
          "maySupportFacts": {
            "type": "boolean",
            "const": false
          },
          "source": {
            "$ref": "#/$defs/activitySource"
          },
          "ref": {
            "$ref": "#/$defs/activityRef"
          }
        }
      },
      "activitySource": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "type",
          "id"
        ],
        "properties": {
          "type": {
            "type": "string",
            "const": "activity_timeline"
          },
          "id": {
            "type": "string",
            "minLength": 1
          }
        }
      },
      "activityRef": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "type",
          "id"
        ],
        "properties": {
          "type": {
            "type": "string",
            "const": "timeline"
          },
          "id": {
            "type": "string",
            "minLength": 1
          }
        }
      },
      "digestItem": {
        "type": "object",
        "required": [
          "evidenceId",
          "sourceKind",
          "text",
          "occurredAtMs"
        ],
        "properties": {
          "evidenceId": {
            "type": "string",
            "minLength": 1
          },
          "sourceKind": {
            "type": "string",
            "minLength": 1
          },
          "text": {
            "type": "string",
            "minLength": 1
          },
          "occurredAtMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      }
    }
  },
  "delivery-gate-observation.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "wisdom-weasel.delivery-gate-observation.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "gateReceiptId",
      "rootId",
      "catalogRevisionId",
      "targetCommit",
      "mode",
      "gateStatus",
      "enforcementApplied",
      "blindReviewStatus",
      "reasons",
      "proofMatrix",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "wisdom-weasel.delivery-gate-observation.v1"
      },
      "gateReceiptId": {
        "type": "string",
        "minLength": 1
      },
      "rootId": {
        "type": "string",
        "minLength": 1
      },
      "catalogRevisionId": {
        "type": "string",
        "minLength": 1
      },
      "targetCommit": {
        "type": "string",
        "minLength": 1
      },
      "mode": {
        "const": "observe_warn"
      },
      "gateStatus": {
        "enum": [
          "observed_pass",
          "warn_blocked"
        ]
      },
      "enforcementApplied": {
        "const": false
      },
      "blindReviewStatus": {
        "enum": [
          "pending",
          "passed",
          "failed",
          "unavailable"
        ]
      },
      "reasons": {
        "type": "array",
        "items": {
          "type": "string"
        }
      },
      "proofMatrix": {
        "type": "array",
        "items": {
          "type": "object"
        }
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      }
    }
  },
  "eval-lab-run-list.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.eval-lab-run-list.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "ok",
      "items",
      "total",
      "experiments",
      "experimentTotal"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.eval-lab-run-list.v1"
      },
      "ok": {
        "const": true
      },
      "items": {
        "type": "array",
        "maxItems": 500,
        "items": {
          "$ref": "#/$defs/run"
        }
      },
      "total": {
        "type": "integer",
        "minimum": 0
      },
      "experiments": {
        "type": "array",
        "maxItems": 500,
        "items": {
          "type": "object",
          "required": [
            "schemaVersion",
            "experimentId",
            "revisionSha256"
          ],
          "properties": {
            "schemaVersion": {
              "const": "rag-ime.agent-lab-experiment.v1"
            },
            "experimentId": {
              "type": "string",
              "minLength": 1
            },
            "revisionSha256": {
              "$ref": "#/$defs/sha256"
            }
          }
        }
      },
      "experimentTotal": {
        "type": "integer",
        "minimum": 0
      },
      "pathSearches": {
        "type": "array",
        "maxItems": 32,
        "items": {
          "$ref": "#/$defs/pathSearch"
        }
      },
      "pathSearchTotal": {
        "type": "integer",
        "minimum": 0
      }
    },
    "$defs": {
      "sha256": {
        "type": "string",
        "pattern": "^[a-f0-9]{64}$"
      },
      "task": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "sessionId",
          "title",
          "taskAlias",
          "taskIndex",
          "taskSucceeded",
          "terminalEvent",
          "verifierPassed",
          "verifierTotal",
          "toolCalls",
          "failedToolCalls",
          "latencyMs"
        ],
        "properties": {
          "sessionId": {
            "type": "string",
            "pattern": "^agent:"
          },
          "title": {
            "type": "string",
            "minLength": 1
          },
          "taskAlias": {
            "type": "string",
            "minLength": 1
          },
          "taskIndex": {
            "type": "integer",
            "minimum": 1
          },
          "taskSucceeded": {
            "type": "boolean"
          },
          "terminalEvent": {
            "type": "string",
            "minLength": 1
          },
          "verifierPassed": {
            "type": "integer",
            "minimum": 0
          },
          "verifierTotal": {
            "type": "integer",
            "minimum": 0
          },
          "toolCalls": {
            "type": "integer",
            "minimum": 0
          },
          "failedToolCalls": {
            "type": "integer",
            "minimum": 0
          },
          "latencyMs": {
            "type": "number",
            "minimum": 0
          },
          "explanation": {
            "$ref": "#/$defs/explanation"
          }
        }
      },
      "explanation": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "caseId",
          "businessRequest",
          "agentOutcome",
          "acceptance"
        ],
        "properties": {
          "caseId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120
          },
          "businessRequest": {
            "$ref": "#/$defs/normalizedText"
          },
          "agentOutcome": {
            "$ref": "#/$defs/normalizedSummary"
          },
          "acceptance": {
            "$ref": "#/$defs/acceptance"
          }
        }
      },
      "normalizedText": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "normalizedText"
        ],
        "properties": {
          "normalizedText": {
            "type": "string",
            "minLength": 1,
            "maxLength": 600
          }
        }
      },
      "normalizedSummary": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "normalizedSummary"
        ],
        "properties": {
          "normalizedSummary": {
            "type": "string",
            "minLength": 1,
            "maxLength": 600
          }
        }
      },
      "acceptance": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "passed",
          "total",
          "items"
        ],
        "properties": {
          "passed": {
            "type": "integer",
            "minimum": 0
          },
          "total": {
            "type": "integer",
            "minimum": 0
          },
          "items": {
            "type": "array",
            "maxItems": 64,
            "items": {
              "$ref": "#/$defs/acceptanceItem"
            }
          }
        }
      },
      "acceptanceItem": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "id",
          "label",
          "status",
          "failureOwner",
          "explanation"
        ],
        "properties": {
          "id": {
            "type": "string",
            "pattern": "^[a-z][a-z0-9._-]{0,79}$"
          },
          "label": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120
          },
          "status": {
            "enum": [
              "pass",
              "fail",
              "partial",
              "unknown"
            ]
          },
          "failureOwner": {
            "enum": [
              null,
              "prompt_context",
              "evaluator_gold",
              "agent",
              "unknown"
            ]
          },
          "explanation": {
            "type": "string",
            "maxLength": 240
          }
        }
      },
      "run": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "schemaVersion",
          "runId",
          "title",
          "suiteId",
          "split",
          "workflowProfile",
          "status",
          "taskCount",
          "taskSuccessCount",
          "taskSuccessRate",
          "verifierPassCount",
          "verifierCount",
          "verifierPassRate",
          "toolCalls",
          "failedToolCalls",
          "latencyMs",
          "sourceDatabaseSha256",
          "sourceReportSha256",
          "createdAtMs",
          "updatedAtMs",
          "tasks"
        ],
        "properties": {
          "schemaVersion": {
            "const": "rag-ime.eval-lab-run.v1"
          },
          "runId": {
            "type": "string",
            "minLength": 1
          },
          "title": {
            "type": "string",
            "minLength": 1
          },
          "suiteId": {
            "type": "string",
            "minLength": 1
          },
          "split": {
            "type": "string",
            "minLength": 1
          },
          "workflowProfile": {
            "type": "string",
            "minLength": 1
          },
          "status": {
            "enum": [
              "completed"
            ]
          },
          "taskCount": {
            "type": "integer",
            "minimum": 1
          },
          "taskSuccessCount": {
            "type": "integer",
            "minimum": 0
          },
          "taskSuccessRate": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          },
          "verifierPassCount": {
            "type": "integer",
            "minimum": 0
          },
          "verifierCount": {
            "type": "integer",
            "minimum": 0
          },
          "verifierPassRate": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          },
          "toolCalls": {
            "type": "integer",
            "minimum": 0
          },
          "failedToolCalls": {
            "type": "integer",
            "minimum": 0
          },
          "latencyMs": {
            "type": "number",
            "minimum": 0
          },
          "sourceDatabaseSha256": {
            "$ref": "#/$defs/sha256"
          },
          "sourceReportSha256": {
            "$ref": "#/$defs/sha256"
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "updatedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "tasks": {
            "type": "array",
            "minItems": 1,
            "maxItems": 500,
            "items": {
              "$ref": "#/$defs/task"
            }
          }
        }
      },
      "pathSearch": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "schemaVersion",
          "searchId",
          "title",
          "objectiveSummary",
          "metricSummary",
          "frozenControlCount",
          "selectedNodeId",
          "selectedPath",
          "claimStatus",
          "claimSummary",
          "candidates",
          "generatedAtMs"
        ],
        "properties": {
          "schemaVersion": {
            "const": "rag-ime.agent-lab-path-search.v1"
          },
          "searchId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "title": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200
          },
          "objectiveSummary": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1600
          },
          "metricSummary": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1600
          },
          "frozenControlCount": {
            "type": "integer",
            "minimum": 0
          },
          "selectedNodeId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "selectedPath": {
            "type": "array",
            "minItems": 1,
            "maxItems": 64,
            "items": {
              "type": "object",
              "additionalProperties": false,
              "required": [
                "nodeId",
                "decision",
                "reason"
              ],
              "properties": {
                "nodeId": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 160
                },
                "decision": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 40
                },
                "reason": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 1600
                }
              }
            }
          },
          "claimStatus": {
            "enum": [
              "best_known",
              "blocked",
              "insufficient_evidence"
            ]
          },
          "claimSummary": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1600
          },
          "candidates": {
            "type": "array",
            "maxItems": 128,
            "items": {
              "type": "object",
              "additionalProperties": false,
              "required": [
                "nodeId",
                "changedFactor",
                "status",
                "metrics",
                "reason"
              ],
              "properties": {
                "nodeId": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 160
                },
                "changedFactor": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 100
                },
                "status": {
                  "enum": [
                    "eligible",
                    "rejected",
                    "not_evaluated",
                    "unknown"
                  ]
                },
                "metrics": {
                  "type": "object",
                  "maxProperties": 64,
                  "additionalProperties": {
                    "type": "number"
                  }
                },
                "reason": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 1600
                }
              }
            }
          },
          "generatedAtMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      }
    }
  },
  "eval-run.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.eval-run.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "evalRunId",
      "traceIds",
      "mode",
      "metricAuthority",
      "truth",
      "evaluator",
      "metrics",
      "status",
      "createdAtMs",
      "updatedAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.eval-run.v1"
      },
      "evalRunId": {
        "type": "string",
        "minLength": 1
      },
      "traceIds": {
        "type": "array",
        "minItems": 1,
        "maxItems": 2048,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "mode": {
        "enum": [
          "ground_truth",
          "ai_judge"
        ]
      },
      "metricAuthority": {
        "enum": [
          "ground_truth",
          "ai_judge_estimate"
        ]
      },
      "truth": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "status",
          "datasetId",
          "labelRevision"
        ],
        "properties": {
          "status": {
            "enum": [
              "none",
              "human",
              "frozen"
            ]
          },
          "datasetId": {
            "type": "string"
          },
          "labelRevision": {
            "type": "string"
          }
        }
      },
      "evaluator": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "provider",
          "model",
          "thinking",
          "displayName"
        ],
        "properties": {
          "provider": {
            "type": "string"
          },
          "model": {
            "type": "string"
          },
          "thinking": {
            "type": "string"
          },
          "displayName": {
            "type": "string"
          }
        }
      },
      "requestedEvaluator": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "provider",
          "model",
          "thinking",
          "displayName"
        ],
        "properties": {
          "provider": {
            "type": "string"
          },
          "model": {
            "type": "string"
          },
          "thinking": {
            "type": "string"
          },
          "displayName": {
            "type": "string"
          }
        }
      },
      "suiteBinding": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "suiteId",
          "suiteRevision"
        ],
        "properties": {
          "suiteId": {
            "type": "string",
            "minLength": 1
          },
          "suiteRevision": {
            "type": "string",
            "minLength": 1
          }
        }
      },
      "metrics": {
        "type": "object",
        "additionalProperties": {
          "type": "number"
        }
      },
      "status": {
        "enum": [
          "queued",
          "running",
          "completed",
          "failed"
        ]
      },
      "promptVersion": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "rubricVersion": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "inputTraceFingerprint": {
        "type": "string",
        "pattern": "^sha256:[0-9a-f]{64}$"
      },
      "startedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "completedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "elapsedMs": {
        "type": "integer",
        "minimum": 0
      },
      "latencyMs": {
        "type": "integer",
        "minimum": 0
      },
      "usage": {
        "type": "object",
        "additionalProperties": false,
        "minProperties": 1,
        "properties": {
          "input": {
            "type": "integer",
            "minimum": 0
          },
          "output": {
            "type": "integer",
            "minimum": 0
          },
          "cacheRead": {
            "type": "integer",
            "minimum": 0
          },
          "cacheWrite": {
            "type": "integer",
            "minimum": 0
          },
          "totalTokens": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "cost": {
        "type": "object",
        "additionalProperties": false,
        "minProperties": 1,
        "properties": {
          "input": {
            "type": "number",
            "minimum": 0
          },
          "output": {
            "type": "number",
            "minimum": 0
          },
          "cacheRead": {
            "type": "number",
            "minimum": 0
          },
          "cacheWrite": {
            "type": "number",
            "minimum": 0
          },
          "total": {
            "type": "number",
            "minimum": 0
          }
        }
      },
      "fallbackUsed": {
        "type": "boolean"
      },
      "failureCode": {
        "enum": [
          "ai_judge_runtime_unavailable",
          "ai_judge_request_failed",
          "ai_judge_invalid_response",
          "ai_judge_timeout"
        ]
      },
      "sourceTraceId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "repairTraceId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "sourceScope": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "failureRef": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "repairReceiptId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "changeReceiptId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "testEvidenceId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "testStatus": {
        "const": "passed"
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "updatedAtMs": {
        "type": "integer",
        "minimum": 0
      }
    }
  },
  "eval-schedule-create.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.eval-schedule-create.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "ok",
      "schedule"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.eval-schedule-create.v1"
      },
      "ok": {
        "const": true
      },
      "schedule": {
        "$ref": "#/$defs/schedule"
      }
    },
    "$defs": {
      "schedule": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "id",
          "suiteId",
          "suiteRevision",
          "recurrenceKind",
          "recurrenceInterval",
          "maxRuns",
          "runCount",
          "status",
          "initialDueAtMs",
          "nextDueAtMs",
          "lastErrorCode",
          "createdAtMs",
          "updatedAtMs",
          "latestRun"
        ],
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1
          },
          "suiteId": {
            "type": "string",
            "minLength": 1
          },
          "suiteRevision": {
            "type": "string",
            "minLength": 1
          },
          "recurrenceKind": {
            "enum": [
              "daily",
              "weekly"
            ]
          },
          "recurrenceInterval": {
            "type": "integer",
            "minimum": 1,
            "maximum": 30
          },
          "maxRuns": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100
          },
          "runCount": {
            "type": "integer",
            "minimum": 0
          },
          "status": {
            "enum": [
              "scheduled",
              "running",
              "completed",
              "failed",
              "paused",
              "cancelled"
            ]
          },
          "initialDueAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "nextDueAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "lastErrorCode": {
            "type": "string"
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "updatedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "latestRun": {
            "$ref": "#/$defs/latestRun"
          }
        }
      },
      "latestRun": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1
          },
          "scheduleId": {
            "type": "string",
            "minLength": 1
          },
          "attempt": {
            "type": "integer",
            "minimum": 1
          },
          "state": {
            "enum": [
              "claimed",
              "succeeded",
              "failed"
            ]
          },
          "dueAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "claimedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "finishedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "evalRunId": {
            "type": "string"
          },
          "errorCode": {
            "type": "string"
          }
        }
      }
    }
  },
  "eval-schedule-error.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.eval-schedule-error.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "ok",
      "errorCode",
      "error"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.eval-schedule-error.v1"
      },
      "ok": {
        "const": false
      },
      "errorCode": {
        "enum": [
          "invalid_request",
          "schedule_not_found",
          "schedule_unavailable"
        ]
      },
      "error": {
        "type": "string",
        "minLength": 1
      }
    }
  },
  "eval-schedule-list.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.eval-schedule-list.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "ok",
      "items"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.eval-schedule-list.v1"
      },
      "ok": {
        "const": true
      },
      "items": {
        "type": "array",
        "maxItems": 500,
        "items": {
          "$ref": "#/$defs/schedule"
        }
      }
    },
    "$defs": {
      "schedule": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "id",
          "suiteId",
          "suiteRevision",
          "recurrenceKind",
          "recurrenceInterval",
          "maxRuns",
          "runCount",
          "status",
          "initialDueAtMs",
          "nextDueAtMs",
          "lastErrorCode",
          "createdAtMs",
          "updatedAtMs",
          "latestRun"
        ],
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1
          },
          "suiteId": {
            "type": "string",
            "minLength": 1
          },
          "suiteRevision": {
            "type": "string",
            "minLength": 1
          },
          "recurrenceKind": {
            "enum": [
              "daily",
              "weekly"
            ]
          },
          "recurrenceInterval": {
            "type": "integer",
            "minimum": 1,
            "maximum": 30
          },
          "maxRuns": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100
          },
          "runCount": {
            "type": "integer",
            "minimum": 0
          },
          "status": {
            "enum": [
              "scheduled",
              "running",
              "completed",
              "failed",
              "paused",
              "cancelled"
            ]
          },
          "initialDueAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "nextDueAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "lastErrorCode": {
            "type": "string"
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "updatedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "latestRun": {
            "$ref": "#/$defs/latestRun"
          }
        }
      },
      "latestRun": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1
          },
          "scheduleId": {
            "type": "string",
            "minLength": 1
          },
          "attempt": {
            "type": "integer",
            "minimum": 1
          },
          "state": {
            "enum": [
              "claimed",
              "succeeded",
              "failed"
            ]
          },
          "dueAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "claimedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "finishedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "evalRunId": {
            "type": "string"
          },
          "errorCode": {
            "type": "string"
          }
        }
      }
    }
  },
  "eval-schedule-run-list.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.eval-schedule-run-list.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "ok",
      "schedule",
      "items"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.eval-schedule-run-list.v1"
      },
      "ok": {
        "const": true
      },
      "schedule": {
        "$ref": "#/$defs/schedule"
      },
      "items": {
        "type": "array",
        "maxItems": 500,
        "items": {
          "$ref": "#/$defs/run"
        }
      }
    },
    "$defs": {
      "schedule": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "id",
          "suiteId",
          "suiteRevision",
          "recurrenceKind",
          "recurrenceInterval",
          "maxRuns",
          "runCount",
          "status",
          "initialDueAtMs",
          "nextDueAtMs",
          "lastErrorCode",
          "createdAtMs",
          "updatedAtMs",
          "latestRun"
        ],
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1
          },
          "suiteId": {
            "type": "string",
            "minLength": 1
          },
          "suiteRevision": {
            "type": "string",
            "minLength": 1
          },
          "recurrenceKind": {
            "enum": [
              "daily",
              "weekly"
            ]
          },
          "recurrenceInterval": {
            "type": "integer",
            "minimum": 1,
            "maximum": 30
          },
          "maxRuns": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100
          },
          "runCount": {
            "type": "integer",
            "minimum": 0
          },
          "status": {
            "enum": [
              "scheduled",
              "running",
              "completed",
              "failed",
              "paused",
              "cancelled"
            ]
          },
          "initialDueAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "nextDueAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "lastErrorCode": {
            "type": "string"
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "updatedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "latestRun": {
            "$ref": "#/$defs/latestRun"
          }
        }
      },
      "latestRun": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1
          },
          "scheduleId": {
            "type": "string",
            "minLength": 1
          },
          "attempt": {
            "type": "integer",
            "minimum": 1
          },
          "state": {
            "enum": [
              "claimed",
              "succeeded",
              "failed"
            ]
          },
          "dueAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "claimedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "finishedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "evalRunId": {
            "type": "string"
          },
          "errorCode": {
            "type": "string"
          }
        }
      },
      "run": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "id",
          "scheduleId",
          "attempt",
          "state",
          "dueAtMs",
          "claimedAtMs",
          "finishedAtMs",
          "evalRunId",
          "errorCode",
          "traceIds",
          "traceIdsTruncated"
        ],
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1
          },
          "scheduleId": {
            "type": "string",
            "minLength": 1
          },
          "attempt": {
            "type": "integer",
            "minimum": 1
          },
          "state": {
            "enum": [
              "claimed",
              "succeeded",
              "failed"
            ]
          },
          "dueAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "claimedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "finishedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "evalRunId": {
            "type": "string"
          },
          "errorCode": {
            "type": "string"
          },
          "traceIds": {
            "type": "array",
            "maxItems": 64,
            "uniqueItems": true,
            "items": {
              "type": "string",
              "minLength": 1
            }
          },
          "traceIdsTruncated": {
            "type": "boolean"
          }
        }
      }
    }
  },
  "eval-schedule.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.eval-schedule.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "id",
      "suiteId",
      "suiteRevision",
      "recurrenceKind",
      "recurrenceInterval",
      "maxRuns",
      "runCount",
      "status",
      "initialDueAtMs",
      "nextDueAtMs",
      "lastErrorCode",
      "createdAtMs",
      "updatedAtMs",
      "latestRun"
    ],
    "properties": {
      "id": {
        "type": "string",
        "minLength": 1
      },
      "suiteId": {
        "type": "string",
        "minLength": 1
      },
      "suiteRevision": {
        "type": "string",
        "minLength": 1
      },
      "recurrenceKind": {
        "enum": [
          "daily",
          "weekly"
        ]
      },
      "recurrenceInterval": {
        "type": "integer",
        "minimum": 1,
        "maximum": 30
      },
      "maxRuns": {
        "type": "integer",
        "minimum": 1,
        "maximum": 100
      },
      "runCount": {
        "type": "integer",
        "minimum": 0
      },
      "status": {
        "enum": [
          "scheduled",
          "running",
          "completed",
          "failed",
          "paused",
          "cancelled"
        ]
      },
      "initialDueAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "nextDueAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "lastErrorCode": {
        "type": "string"
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "updatedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "latestRun": {
        "$ref": "#/$defs/latestRun"
      }
    },
    "$defs": {
      "latestRun": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1
          },
          "scheduleId": {
            "type": "string",
            "minLength": 1
          },
          "attempt": {
            "type": "integer",
            "minimum": 1
          },
          "state": {
            "enum": [
              "claimed",
              "succeeded",
              "failed"
            ]
          },
          "dueAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "claimedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "finishedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "evalRunId": {
            "type": "string"
          },
          "errorCode": {
            "type": "string"
          }
        }
      }
    }
  },
  "eval-suite-list.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.eval-suite-list.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "ok",
      "items"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.eval-suite-list.v1"
      },
      "ok": {
        "const": true
      },
      "items": {
        "type": "array",
        "maxItems": 100,
        "items": {
          "$ref": "#/$defs/suite"
        }
      }
    },
    "$defs": {
      "suite": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "suiteId",
          "suiteRevision",
          "displayName",
          "fixtureCount",
          "capabilities"
        ],
        "properties": {
          "suiteId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200
          },
          "suiteRevision": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120
          },
          "displayName": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "fixtureCount": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10000
          },
          "capabilities": {
            "type": "array",
            "maxItems": 16,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 120
            }
          }
        }
      }
    }
  },
  "foreground-commit.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.foreground-commit.v1",
    "type": "object",
    "required": [
      "text"
    ],
    "properties": {
      "text": {
        "type": "string",
        "minLength": 1
      },
      "recentContext": {
        "type": "string"
      },
      "preedit": {
        "type": "string"
      },
      "project": {
        "type": "string"
      },
      "app": {
        "type": "string"
      },
      "frontAppBundleId": {
        "type": "string"
      },
      "frontmostApp": {
        "type": "string"
      },
      "bundleId": {
        "type": "string"
      },
      "candidateRank": {
        "type": [
          "integer",
          "null"
        ]
      },
      "providerName": {
        "type": "string"
      },
      "tags": {
        "type": "array",
        "items": {
          "type": "string"
        }
      },
      "source": {
        "type": "string"
      },
      "contextGroupId": {
        "type": "string"
      },
      "contextGroupLevel": {
        "type": "string"
      },
      "captureMetadata": {
        "oneOf": [
          {
            "type": "object",
            "properties": {
              "captureSource": {
                "type": "string"
              },
              "fallbackReason": {
                "type": "string"
              },
              "fieldContextChars": {
                "type": "integer",
                "minimum": 0
              },
              "imeBufferChars": {
                "type": "integer",
                "minimum": 0
              },
              "selectedTextSha256": {
                "type": "string"
              },
              "selectionRule": {
                "type": "string"
              }
            },
            "additionalProperties": false
          },
          {
            "type": "object",
            "required": [
              "schemaVersion",
              "captureId",
              "transactionId",
              "sequence",
              "channel",
              "boundaryKind",
              "boundaryConfidence",
              "nativeCompositionBefore",
              "rimeHandled",
              "hostForwarded",
              "modifiedReturn",
              "finalCommitted",
              "controllerEpoch",
              "focusEpoch",
              "appBundleId",
              "fieldIdentitySha256",
              "privacyRevision",
              "occurredStartMs",
              "occurredEndMs",
              "contentSha256",
              "captureSource",
              "fallbackReason",
              "fieldContextChars",
              "imeBufferChars",
              "selectionRule"
            ],
            "properties": {
              "schemaVersion": {
                "const": "rag-ime.input-capture.v2"
              },
              "captureId": {
                "type": "string",
                "minLength": 1,
                "maxLength": 160
              },
              "transactionId": {
                "type": "string",
                "minLength": 1,
                "maxLength": 160
              },
              "sequence": {
                "type": "integer",
                "minimum": 1
              },
              "channel": {
                "type": "string",
                "enum": [
                  "input_method",
                  "voice"
                ]
              },
              "boundaryKind": {
                "type": "string",
                "enum": [
                  "host_return",
                  "focus_change",
                  "app_change",
                  "deactivate",
                  "voice_final"
                ]
              },
              "boundaryConfidence": {
                "type": "string",
                "enum": [
                  "strong",
                  "weak"
                ]
              },
              "nativeCompositionBefore": {
                "type": "boolean"
              },
              "rimeHandled": {
                "type": "boolean"
              },
              "hostForwarded": {
                "type": "boolean"
              },
              "modifiedReturn": {
                "type": "boolean"
              },
              "finalCommitted": {
                "type": "boolean"
              },
              "controllerEpoch": {
                "type": "integer",
                "minimum": 0
              },
              "focusEpoch": {
                "type": "integer",
                "minimum": 0
              },
              "appBundleId": {
                "type": "string",
                "minLength": 1,
                "maxLength": 300
              },
              "fieldIdentitySha256": {
                "type": "string",
                "pattern": "^[0-9a-fA-F]{64}$"
              },
              "privacyRevision": {
                "type": "string",
                "minLength": 1,
                "maxLength": 120
              },
              "occurredStartMs": {
                "type": "integer",
                "minimum": 0
              },
              "occurredEndMs": {
                "type": "integer",
                "minimum": 0
              },
              "contentSha256": {
                "type": "string",
                "pattern": "^[0-9a-fA-F]{64}$"
              },
              "captureSource": {
                "type": "string",
                "enum": [
                  "text_input_client",
                  "accessibility",
                  "ime_active_buffer",
                  "voice_insertion"
                ]
              },
              "fallbackReason": {
                "type": "string",
                "maxLength": 120
              },
              "fieldContextChars": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100000
              },
              "imeBufferChars": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100000
              },
              "selectionRule": {
                "type": "string",
                "maxLength": 120
              }
            },
            "additionalProperties": false
          }
        ]
      },
      "privacyDisposition": {
        "type": "string",
        "enum": [
          "allowed",
          "sensitive",
          "unknown"
        ]
      },
      "sensitiveField": {
        "type": "boolean"
      },
      "secureInput": {
        "type": "boolean"
      }
    },
    "additionalProperties": false
  },
  "foreground-context.v2": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.foreground-context.v2",
    "type": "object",
    "required": [
      "available",
      "source",
      "freshnessMs",
      "canReplaceSelection"
    ],
    "properties": {
      "available": {
        "type": "boolean"
      },
      "source": {
        "type": "string"
      },
      "freshnessMs": {
        "type": "integer",
        "minimum": 0
      },
      "capturedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "surroundingBefore": {
        "type": "string"
      },
      "surroundingAfter": {
        "type": "string"
      },
      "selectedText": {
        "type": "string"
      },
      "selectedTextHash": {
        "type": "string"
      },
      "canReplaceSelection": {
        "type": "boolean"
      },
      "contextGroupId": {
        "type": "string"
      },
      "contextGroupLevel": {
        "type": "string",
        "enum": [
          "document",
          "project",
          "app",
          "global",
          ""
        ]
      }
    }
  },
  "frontend-capabilities.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.frontend-capabilities.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "gatewayVersion",
      "contracts",
      "features",
      "adapterBoundary",
      "legacyCompatibility"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.frontend-capabilities.v1"
      },
      "gatewayVersion": {
        "type": "string",
        "const": "rag-ime.frontend-gateway.v1"
      },
      "contracts": {
        "type": "object"
      },
      "features": {
        "type": "object"
      },
      "adapterBoundary": {
        "type": "object"
      },
      "legacyCompatibility": {
        "type": "object"
      }
    }
  },
  "frontend-selection-response.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.frontend-selection-response.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "gatewayVersion",
      "ok",
      "session",
      "selectionReceipt",
      "eventId",
      "origin",
      "insertText",
      "recordedActionCount",
      "privacy",
      "backendContract"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.frontend-selection-response.v1"
      },
      "gatewayVersion": {
        "type": "string",
        "const": "rag-ime.frontend-gateway.v1"
      },
      "ok": {
        "type": "boolean"
      },
      "session": {
        "$ref": "#/$defs/session"
      },
      "selectionReceipt": {
        "$ref": "#/$defs/selectionReceipt"
      },
      "eventId": {
        "type": "string"
      },
      "origin": {
        "type": "string"
      },
      "insertText": {
        "type": "string"
      },
      "recordedActionCount": {
        "type": "integer",
        "minimum": 0
      },
      "privacy": {
        "type": "object"
      },
      "backendContract": {
        "type": "string"
      }
    },
    "$defs": {
      "session": {
        "type": "object",
        "required": [
          "id",
          "requestSeq",
          "inputGeneration"
        ],
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1
          },
          "requestSeq": {
            "type": "integer",
            "minimum": 0
          },
          "inputGeneration": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "selectionReceipt": {
        "type": "object",
        "required": [
          "sessionId",
          "requestSeq",
          "inputGeneration",
          "snapshotId",
          "snapshotGeneration",
          "candidateId",
          "requestConsistencyValidated",
          "runtimeFreshnessValidated"
        ],
        "properties": {
          "sessionId": {
            "type": "string",
            "minLength": 1
          },
          "requestSeq": {
            "type": "integer",
            "minimum": 0
          },
          "inputGeneration": {
            "type": "integer",
            "minimum": 0
          },
          "snapshotId": {
            "type": "string",
            "minLength": 1
          },
          "snapshotGeneration": {
            "type": "integer",
            "minimum": 0
          },
          "candidateId": {
            "type": "string",
            "minLength": 1
          },
          "requestConsistencyValidated": {
            "type": "boolean"
          },
          "runtimeFreshnessValidated": {
            "type": "boolean"
          }
        }
      }
    }
  },
  "frontend-selection.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.frontend-selection.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "frontend",
      "session",
      "privacy",
      "candidate"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.frontend-selection.v1"
      },
      "frontend": {
        "type": "object"
      },
      "session": {
        "$ref": "#/$defs/session"
      },
      "privacy": {
        "type": "object",
        "required": [
          "disposition"
        ],
        "properties": {
          "disposition": {
            "type": "string",
            "enum": [
              "allowed",
              "sensitive",
              "unknown"
            ]
          },
          "reason": {
            "type": "string"
          },
          "sensitiveField": {
            "type": "boolean"
          },
          "secureInput": {
            "type": "boolean"
          }
        }
      },
      "candidate": {
        "$ref": "#/$defs/candidate"
      },
      "visibleCandidates": {
        "type": "array",
        "items": {
          "$ref": "#/$defs/candidate"
        }
      },
      "context": {
        "type": "object"
      },
      "providerName": {
        "type": "string"
      },
      "dryRun": {
        "type": "boolean"
      }
    },
    "$defs": {
      "candidate": {
        "type": "object",
        "required": [
          "id",
          "snapshotId",
          "snapshotGeneration",
          "inputGeneration",
          "text",
          "insertText",
          "origin"
        ],
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1
          },
          "snapshotId": {
            "type": "string",
            "minLength": 1
          },
          "snapshotGeneration": {
            "type": "integer",
            "minimum": 0
          },
          "inputGeneration": {
            "type": "integer",
            "minimum": 0
          },
          "label": {
            "type": "string"
          },
          "text": {
            "type": "string"
          },
          "insertText": {
            "type": "string",
            "minLength": 1
          },
          "origin": {
            "type": "string",
            "enum": [
              "native",
              "model",
              "retrieval",
              "memory",
              "action",
              "status",
              "literal",
              "assistant"
            ]
          },
          "rank": {
            "type": "integer",
            "minimum": 1
          },
          "nativeIndex": {
            "type": [
              "integer",
              "null"
            ]
          },
          "selectionAction": {
            "type": "string"
          },
          "suggestionId": {
            "type": "string"
          },
          "memoryId": {
            "type": "string"
          },
          "sourceEventId": {
            "type": [
              "integer",
              "null"
            ]
          },
          "metadata": {
            "type": "object"
          }
        }
      },
      "session": {
        "type": "object",
        "required": [
          "id",
          "requestSeq",
          "inputGeneration"
        ],
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1
          },
          "requestSeq": {
            "type": "integer",
            "minimum": 0
          },
          "inputGeneration": {
            "type": "integer",
            "minimum": 0
          }
        }
      }
    }
  },
  "frontend-suggest-request.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.frontend-suggest-request.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "frontend",
      "session",
      "privacy",
      "input"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.frontend-suggest-request.v1"
      },
      "frontend": {
        "$ref": "#/$defs/frontend"
      },
      "session": {
        "$ref": "#/$defs/session"
      },
      "privacy": {
        "$ref": "#/$defs/privacy"
      },
      "input": {
        "$ref": "#/$defs/input"
      },
      "context": {
        "type": "object"
      },
      "nativeCandidates": {
        "type": "array",
        "items": {
          "$ref": "#/$defs/nativeCandidate"
        }
      },
      "nativeState": {
        "type": "object"
      },
      "limits": {
        "type": "object"
      },
      "flags": {
        "type": "object"
      }
    },
    "$defs": {
      "frontend": {
        "type": "object",
        "required": [
          "id",
          "platform",
          "inputEngine"
        ],
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1
          },
          "build": {
            "type": "string"
          },
          "platform": {
            "type": "string",
            "minLength": 1
          },
          "inputFramework": {
            "type": "string"
          },
          "inputEngine": {
            "type": "string",
            "minLength": 1
          }
        }
      },
      "session": {
        "type": "object",
        "required": [
          "id",
          "requestSeq",
          "inputGeneration"
        ],
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1
          },
          "requestSeq": {
            "type": "integer",
            "minimum": 0
          },
          "inputGeneration": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "privacy": {
        "type": "object",
        "required": [
          "disposition"
        ],
        "properties": {
          "disposition": {
            "type": "string",
            "enum": [
              "allowed",
              "sensitive",
              "unknown"
            ]
          },
          "reason": {
            "type": "string"
          },
          "sensitiveField": {
            "type": "boolean"
          },
          "secureInput": {
            "type": "boolean"
          }
        }
      },
      "input": {
        "type": "object",
        "properties": {
          "raw": {
            "type": "string"
          },
          "preedit": {
            "type": "string"
          },
          "commitPreview": {
            "type": "string"
          },
          "committedContext": {
            "type": "string"
          },
          "idleMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "nativeCandidate": {
        "type": "object",
        "required": [
          "text"
        ],
        "properties": {
          "id": {
            "type": "string"
          },
          "label": {
            "type": "string"
          },
          "text": {
            "type": "string",
            "minLength": 1
          },
          "annotation": {
            "type": "string"
          },
          "rank": {
            "type": "integer",
            "minimum": 1
          },
          "nativeIndex": {
            "type": "integer",
            "minimum": 0
          },
          "metadata": {
            "type": "object"
          }
        }
      }
    }
  },
  "frontend-suggest-response.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.frontend-suggest-response.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "gatewayVersion",
      "frontend",
      "session",
      "input",
      "candidates",
      "presentation",
      "selectionPolicy",
      "predictionSession",
      "privacy"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.frontend-suggest-response.v1"
      },
      "gatewayVersion": {
        "type": "string",
        "const": "rag-ime.frontend-gateway.v1"
      },
      "frontend": {
        "type": "object"
      },
      "session": {
        "$ref": "#/$defs/session"
      },
      "input": {
        "type": "object"
      },
      "candidates": {
        "type": "array",
        "items": {
          "$ref": "#/$defs/candidate"
        }
      },
      "presentation": {
        "type": "object"
      },
      "selectionPolicy": {
        "type": "object"
      },
      "predictionSession": {
        "type": "object"
      },
      "progressive": {
        "type": "object"
      },
      "privacy": {
        "type": "object"
      },
      "diagnostics": {
        "type": "object"
      }
    },
    "$defs": {
      "candidate": {
        "type": "object",
        "required": [
          "id",
          "snapshotId",
          "snapshotGeneration",
          "inputGeneration",
          "text",
          "insertText",
          "origin",
          "rank",
          "selectionAction"
        ],
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1
          },
          "snapshotId": {
            "type": "string",
            "minLength": 1
          },
          "snapshotGeneration": {
            "type": "integer",
            "minimum": 0
          },
          "inputGeneration": {
            "type": "integer",
            "minimum": 0
          },
          "label": {
            "type": "string"
          },
          "text": {
            "type": "string"
          },
          "insertText": {
            "type": "string"
          },
          "origin": {
            "type": "string",
            "enum": [
              "native",
              "model",
              "retrieval",
              "memory",
              "action",
              "status",
              "literal",
              "assistant"
            ]
          },
          "provider": {
            "type": "string"
          },
          "rank": {
            "type": "integer",
            "minimum": 1
          },
          "nativeIndex": {
            "type": [
              "integer",
              "null"
            ]
          },
          "selectionAction": {
            "type": "string"
          },
          "metadata": {
            "type": "object"
          }
        }
      },
      "session": {
        "type": "object",
        "required": [
          "id",
          "requestSeq",
          "inputGeneration"
        ],
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1
          },
          "requestSeq": {
            "type": "integer",
            "minimum": 0
          },
          "inputGeneration": {
            "type": "integer",
            "minimum": 0
          }
        }
      }
    }
  },
  "guard-activation-projection.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "wisdom-weasel.guard-activation-projection.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "activationReceiptId",
      "scopeKey",
      "guardCandidateId",
      "guardEpoch",
      "appliesToNewRootsAfterMs",
      "evalRunIds",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "wisdom-weasel.guard-activation-projection.v1"
      },
      "activationReceiptId": {
        "type": "string"
      },
      "scopeKey": {
        "type": "string"
      },
      "guardCandidateId": {
        "type": "string"
      },
      "guardEpoch": {
        "type": "integer"
      },
      "appliesToNewRootsAfterMs": {
        "type": "integer"
      },
      "evalRunIds": {
        "type": "array"
      },
      "createdAtMs": {
        "type": "integer"
      }
    }
  },
  "guard-active-pointer-projection.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "wisdom-weasel.guard-active-pointer-projection.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "scopeKey",
      "guardEpoch",
      "activeGuardCandidateId",
      "activationReceiptId",
      "updatedAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "wisdom-weasel.guard-active-pointer-projection.v1"
      },
      "scopeKey": {
        "type": "string"
      },
      "guardEpoch": {
        "type": "integer"
      },
      "activeGuardCandidateId": {
        "type": [
          "string",
          "null"
        ]
      },
      "activationReceiptId": {
        "type": [
          "string",
          "null"
        ]
      },
      "updatedAtMs": {
        "type": "integer"
      }
    }
  },
  "guard-approval-projection.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "wisdom-weasel.guard-approval-projection.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "approvalReceiptId",
      "guardCandidateId",
      "authorityRef",
      "decision",
      "candidateHash",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "wisdom-weasel.guard-approval-projection.v1"
      },
      "approvalReceiptId": {
        "type": "string"
      },
      "guardCandidateId": {
        "type": "string"
      },
      "authorityRef": {
        "type": "string"
      },
      "decision": {
        "type": "string"
      },
      "candidateHash": {
        "type": "string"
      },
      "createdAtMs": {
        "type": "integer"
      }
    }
  },
  "guard-candidate-projection.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "wisdom-weasel.guard-candidate-projection.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "guardCandidateId",
      "lessonCandidateId",
      "version",
      "condition",
      "action",
      "scope",
      "risk",
      "thresholds",
      "owner",
      "sunsetAtMs",
      "candidateHash",
      "state",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "wisdom-weasel.guard-candidate-projection.v1"
      },
      "guardCandidateId": {
        "type": "string"
      },
      "lessonCandidateId": {
        "type": "string"
      },
      "version": {
        "type": "integer"
      },
      "condition": {
        "type": "object"
      },
      "action": {
        "type": "object"
      },
      "scope": {
        "type": "object"
      },
      "risk": {
        "type": "string"
      },
      "thresholds": {
        "type": "object"
      },
      "owner": {
        "type": "string"
      },
      "sunsetAtMs": {
        "type": "integer"
      },
      "candidateHash": {
        "type": "string"
      },
      "state": {
        "const": "candidate_only"
      },
      "createdAtMs": {
        "type": "integer"
      }
    }
  },
  "guard-eval-run-projection.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "wisdom-weasel.guard-eval-run-projection.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "evalRunId",
      "guardCandidateId",
      "mode",
      "datasetHash",
      "metrics",
      "status",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "wisdom-weasel.guard-eval-run-projection.v1"
      },
      "evalRunId": {
        "type": "string"
      },
      "guardCandidateId": {
        "type": "string"
      },
      "mode": {
        "type": "string"
      },
      "datasetHash": {
        "type": "string"
      },
      "metrics": {
        "type": "object"
      },
      "status": {
        "type": "string"
      },
      "createdAtMs": {
        "type": "integer"
      }
    }
  },
  "guard-materialization-status-projection.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "wisdom-weasel.guard-materialization-status-projection.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "materializationReceiptId",
      "guardCandidateId",
      "guardEpoch",
      "artifactKind",
      "status",
      "artifactHash",
      "projectionRef",
      "errorCode",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "wisdom-weasel.guard-materialization-status-projection.v1"
      },
      "materializationReceiptId": {
        "type": "string"
      },
      "guardCandidateId": {
        "type": "string"
      },
      "guardEpoch": {
        "type": "integer"
      },
      "artifactKind": {
        "type": "string"
      },
      "status": {
        "type": "string"
      },
      "artifactHash": {
        "type": "string"
      },
      "projectionRef": {
        "type": "string"
      },
      "errorCode": {
        "type": "string"
      },
      "createdAtMs": {
        "type": "integer"
      }
    }
  },
  "guard-rollback-projection.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "wisdom-weasel.guard-rollback-projection.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "rollbackReceiptId",
      "scopeKey",
      "fromGuardCandidateId",
      "restoredGuardCandidateId",
      "guardEpoch",
      "cancelledDispatchIds",
      "authorityRef",
      "reason",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "wisdom-weasel.guard-rollback-projection.v1"
      },
      "rollbackReceiptId": {
        "type": "string"
      },
      "scopeKey": {
        "type": "string"
      },
      "fromGuardCandidateId": {
        "type": "string"
      },
      "restoredGuardCandidateId": {
        "type": [
          "string",
          "null"
        ]
      },
      "guardEpoch": {
        "type": "integer"
      },
      "cancelledDispatchIds": {
        "type": "array"
      },
      "authorityRef": {
        "type": "string"
      },
      "reason": {
        "type": "string"
      },
      "createdAtMs": {
        "type": "integer"
      }
    }
  },
  "incident-occurrence-projection.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "wisdom-weasel.incident-occurrence-projection.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "incidentId",
      "taxonomy",
      "failureSignature",
      "evidenceRefs",
      "occurrenceCount",
      "lastObservedAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "wisdom-weasel.incident-occurrence-projection.v1"
      },
      "incidentId": {
        "type": "string"
      },
      "taxonomy": {
        "type": "string"
      },
      "failureSignature": {
        "type": "string"
      },
      "evidenceRefs": {
        "type": "array",
        "items": {
          "type": "string"
        }
      },
      "occurrenceCount": {
        "type": "integer",
        "minimum": 1
      },
      "lastObservedAtMs": {
        "type": "integer",
        "minimum": 0
      }
    }
  },
  "knowledge-document-detail.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://rag-ime.local/contracts/knowledge-document-detail.v1.json",
    "title": "RAG-IME Knowledge Document Detail",
    "type": "object",
    "required": [
      "schemaVersion",
      "document",
      "chunks",
      "pages",
      "assets",
      "tables",
      "artifact",
      "contentWindow"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.knowledge-library.v1"
      },
      "document": {
        "type": "object",
        "required": [
          "documentId",
          "kbId",
          "fileName",
          "mimeType",
          "byteSize",
          "sha256",
          "status",
          "sourceReadPath"
        ],
        "properties": {
          "documentId": {
            "type": "string",
            "minLength": 1
          },
          "kbId": {
            "type": "string",
            "minLength": 1
          },
          "fileName": {
            "type": "string",
            "minLength": 1
          },
          "mimeType": {
            "type": "string",
            "minLength": 1
          },
          "byteSize": {
            "type": "integer",
            "minimum": 0
          },
          "sha256": {
            "type": "string",
            "pattern": "^[a-f0-9]{64}$"
          },
          "status": {
            "enum": [
              "queued",
              "parsing",
              "indexing",
              "ready",
              "stale",
              "failed",
              "deleting"
            ]
          },
          "pageCount": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100000
          },
          "sourceReadPath": {
            "type": "string",
            "pattern": "^/api/knowledge-bases/"
          }
        },
        "additionalProperties": true
      },
      "chunks": {
        "type": "object",
        "required": [
          "items",
          "offset",
          "limit",
          "total",
          "hasMore"
        ],
        "properties": {
          "items": {
            "type": "array"
          },
          "offset": {
            "type": "integer",
            "minimum": 0
          },
          "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500
          },
          "total": {
            "type": "integer",
            "minimum": 0
          },
          "hasMore": {
            "type": "boolean"
          }
        },
        "additionalProperties": false
      },
      "pages": {
        "type": "array"
      },
      "assets": {
        "type": "array",
        "items": {
          "type": "object",
          "required": [
            "assetId",
            "name",
            "mimeType",
            "byteSize",
            "sha256",
            "readPath"
          ],
          "properties": {
            "assetId": {
              "type": "string",
              "pattern": "^[a-f0-9]{64}$"
            },
            "name": {
              "type": "string"
            },
            "mimeType": {
              "enum": [
                "image/png",
                "image/jpeg",
                "image/gif",
                "image/webp",
                "image/bmp"
              ]
            },
            "byteSize": {
              "type": "integer",
              "minimum": 0,
              "maximum": 26214400
            },
            "sha256": {
              "type": "string",
              "pattern": "^[a-f0-9]{64}$"
            },
            "readPath": {
              "type": "string",
              "pattern": "^/api/knowledge-bases/"
            }
          },
          "additionalProperties": false
        }
      },
      "tables": {
        "type": "array",
        "maxItems": 32,
        "items": {
          "type": "object",
          "required": [
            "tableId",
            "title",
            "page",
            "columns",
            "rows",
            "markdown"
          ],
          "properties": {
            "tableId": {
              "type": "string",
              "minLength": 1
            },
            "title": {
              "type": "string",
              "maxLength": 300
            },
            "page": {
              "type": [
                "integer",
                "null"
              ],
              "minimum": 1
            },
            "columns": {
              "type": "array",
              "maxItems": 32,
              "items": {
                "type": "string",
                "maxLength": 500
              }
            },
            "rows": {
              "type": "array",
              "maxItems": 200,
              "items": {
                "type": "array",
                "maxItems": 32,
                "items": {
                  "type": "string",
                  "maxLength": 500
                }
              }
            },
            "markdown": {
              "type": "string",
              "maxLength": 64000
            }
          },
          "additionalProperties": false
        }
      },
      "artifact": {
        "type": "object"
      },
      "contentWindow": {
        "type": "object"
      }
    },
    "additionalProperties": false
  },
  "knowledge-document-import.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://rag-ime.local/contracts/knowledge-document-import.v1.json",
    "title": "RAG-IME Knowledge Document Import Receipt",
    "type": "object",
    "required": [
      "schemaVersion",
      "ok",
      "receipt"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.knowledge-document-import.v1"
      },
      "ok": {
        "const": true
      },
      "receipt": {
        "type": "object",
        "required": [
          "kbId",
          "documentId",
          "fileName",
          "mimeType",
          "byteSize",
          "sha256",
          "status"
        ],
        "properties": {
          "kbId": {
            "type": "string",
            "minLength": 1
          },
          "documentId": {
            "type": "string",
            "minLength": 1
          },
          "fileName": {
            "type": "string",
            "minLength": 1
          },
          "mimeType": {
            "type": "string",
            "minLength": 1
          },
          "byteSize": {
            "type": "integer",
            "minimum": 0
          },
          "sha256": {
            "type": "string",
            "pattern": "^[a-f0-9]{64}$"
          },
          "status": {
            "enum": [
              "queued",
              "parsing",
              "indexing",
              "ready",
              "stale",
              "failed",
              "deleting"
            ]
          }
        },
        "additionalProperties": false
      }
    },
    "additionalProperties": false
  },
  "knowledge-graph.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.knowledge-graph.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "kbId",
      "revision",
      "sourceRevision",
      "status",
      "updatedAtMs",
      "nodes",
      "edges",
      "stats",
      "truncated"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.knowledge-graph.v1"
      },
      "kbId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "revision": {
        "type": "integer",
        "minimum": 0
      },
      "sourceRevision": {
        "type": "string",
        "pattern": "^sha256:[0-9a-f]{64}$"
      },
      "status": {
        "type": "string",
        "enum": [
          "ready",
          "building",
          "stale",
          "failed"
        ]
      },
      "updatedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "jobId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "error": {
        "type": "string",
        "minLength": 1,
        "maxLength": 2000
      },
      "extractor": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "mode",
          "model",
          "configured",
          "degraded"
        ],
        "properties": {
          "mode": {
            "type": "string",
            "enum": [
              "deterministic",
              "model"
            ]
          },
          "model": {
            "type": "string",
            "maxLength": 160
          },
          "configured": {
            "type": "boolean"
          },
          "degraded": {
            "type": "boolean"
          },
          "fingerprint": {
            "type": "string",
            "maxLength": 160
          },
          "processedChunkCount": {
            "type": "integer",
            "minimum": 0
          },
          "cachedChunkCount": {
            "type": "integer",
            "minimum": 0
          },
          "modelChunkCount": {
            "type": "integer",
            "minimum": 0
          },
          "fallbackChunkCount": {
            "type": "integer",
            "minimum": 0
          },
          "errorCount": {
            "type": "integer",
            "minimum": 0
          },
          "batchSize": {
            "type": "integer",
            "minimum": 1,
            "maximum": 8
          },
          "batchCount": {
            "type": "integer",
            "minimum": 0
          },
          "extractionConcurrency": {
            "type": "integer",
            "minimum": 1,
            "maximum": 4
          },
          "effectiveExtractionConcurrency": {
            "type": "integer",
            "minimum": 0,
            "maximum": 4
          },
          "entityCount": {
            "type": "integer",
            "minimum": 0
          },
          "termCount": {
            "type": "integer",
            "minimum": 0
          },
          "topicCount": {
            "type": "integer",
            "minimum": 0
          },
          "relationCount": {
            "type": "integer",
            "minimum": 0
          },
          "lastError": {
            "type": "string",
            "maxLength": 500
          }
        }
      },
      "nodes": {
        "type": "array",
        "maxItems": 1000,
        "items": {
          "$ref": "#/$defs/node"
        }
      },
      "edges": {
        "type": "array",
        "maxItems": 3000,
        "items": {
          "$ref": "#/$defs/edge"
        }
      },
      "stats": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "nodeCount",
          "edgeCount",
          "documentCount",
          "chunkCount",
          "indexedDocumentCount",
          "pendingDocumentCount"
        ],
        "properties": {
          "nodeCount": {
            "type": "integer",
            "minimum": 0
          },
          "edgeCount": {
            "type": "integer",
            "minimum": 0
          },
          "documentCount": {
            "type": "integer",
            "minimum": 0
          },
          "chunkCount": {
            "type": "integer",
            "minimum": 0
          },
          "indexedDocumentCount": {
            "type": "integer",
            "minimum": 0
          },
          "pendingDocumentCount": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "truncated": {
        "type": "boolean"
      }
    },
    "$defs": {
      "node": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "id",
          "label",
          "kind",
          "weight"
        ],
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "label": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "kind": {
            "type": "string",
            "enum": [
              "document",
              "chunk",
              "topic",
              "entity",
              "term"
            ]
          },
          "documentId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "documentName": {
            "type": "string",
            "minLength": 1,
            "maxLength": 512
          },
          "documentIds": {
            "type": "array",
            "maxItems": 1000,
            "uniqueItems": true,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 160
            }
          },
          "chunkId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "heading": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500
          },
          "excerpt": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "page": {
            "type": "integer",
            "minimum": 0
          },
          "weight": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          }
        }
      },
      "edge": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "id",
          "source",
          "target",
          "kind",
          "weight"
        ],
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "source": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "target": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "kind": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64
          },
          "label": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120
          },
          "weight": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          }
        }
      }
    }
  },
  "knowledge-library.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://rag-ime.local/contracts/knowledge-library.v1.json",
    "title": "RAG-IME Document Knowledge Library",
    "type": "object",
    "required": [
      "schemaVersion"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.knowledge-library.v1"
      },
      "bases": {
        "type": "array",
        "items": {
          "type": "object",
          "required": [
            "id",
            "name",
            "parserMode",
            "agentEnabled"
          ],
          "properties": {
            "id": {
              "type": "string",
              "minLength": 1
            },
            "name": {
              "type": "string",
              "minLength": 1
            },
            "description": {
              "type": "string"
            },
            "parserMode": {
              "enum": [
                "auto",
                "builtin",
                "mineru"
              ]
            },
            "agentEnabled": {
              "type": "boolean"
            },
            "chunkingConfig": {
              "$ref": "#/$defs/chunkingConfig"
            },
            "retrievalConfig": {
              "$ref": "#/$defs/retrievalConfig"
            },
            "configRevision": {
              "type": "integer",
              "minimum": 1
            }
          },
          "additionalProperties": true
        }
      },
      "items": {
        "type": "array"
      }
    },
    "$defs": {
      "chunkingConfig": {
        "type": "object",
        "required": [
          "strategy",
          "size",
          "overlap",
          "separator",
          "respectHeadings",
          "respectPageBoundaries"
        ],
        "properties": {
          "strategy": {
            "enum": [
              "general",
              "markdown",
              "book",
              "qa",
              "laws",
              "separator",
              "fixed"
            ]
          },
          "size": {
            "type": "integer",
            "minimum": 200,
            "maximum": 8000
          },
          "overlap": {
            "type": "integer",
            "minimum": 0,
            "maximum": 2000
          },
          "separator": {
            "type": "string",
            "maxLength": 100
          },
          "respectHeadings": {
            "type": "boolean"
          },
          "respectPageBoundaries": {
            "type": "boolean"
          }
        },
        "additionalProperties": false
      },
      "retrievalConfig": {
        "type": "object",
        "required": [
          "mode",
          "topK",
          "threshold",
          "lexicalWeight",
          "denseWeight",
          "rrfK",
          "candidateMultiplier"
        ],
        "properties": {
          "mode": {
            "enum": [
              "lexical",
              "hybrid",
              "dense"
            ]
          },
          "topK": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100
          },
          "threshold": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          },
          "lexicalWeight": {
            "type": "number",
            "minimum": 0,
            "maximum": 10
          },
          "denseWeight": {
            "type": "number",
            "minimum": 0,
            "maximum": 10
          },
          "rrfK": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1000
          },
          "candidateMultiplier": {
            "type": "integer",
            "minimum": 1,
            "maximum": 20
          }
        },
        "additionalProperties": false
      }
    },
    "additionalProperties": true
  },
  "knowledge-search-use-eval-run.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "wisdom-weasel.knowledge-search-use-eval-run.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "evalRunId",
      "datasetId",
      "datasetContentHash",
      "roomBindingId",
      "traceCount",
      "metrics",
      "strataMetrics",
      "status",
      "failureReasons",
      "reportOnly",
      "evaluatorId",
      "contentHash",
      "evaluatorSignature",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "wisdom-weasel.knowledge-search-use-eval-run.v1"
      },
      "evalRunId": {
        "type": "string"
      },
      "datasetId": {
        "type": "string"
      },
      "datasetContentHash": {
        "type": "string"
      },
      "roomBindingId": {
        "type": "string"
      },
      "traceCount": {
        "type": "integer"
      },
      "metrics": {
        "type": "object"
      },
      "strataMetrics": {
        "type": "object"
      },
      "status": {
        "enum": [
          "passed",
          "failed"
        ]
      },
      "failureReasons": {
        "type": "array"
      },
      "reportOnly": {
        "const": true
      },
      "evaluatorId": {
        "type": "string"
      },
      "contentHash": {
        "type": "string"
      },
      "evaluatorSignature": {
        "type": "string"
      },
      "createdAtMs": {
        "type": "integer"
      }
    }
  },
  "lesson-candidate-projection.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "wisdom-weasel.lesson-candidate-projection.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "lessonCandidateId",
      "incidentId",
      "facts",
      "causes",
      "applicabilityBoundary",
      "counterexamples",
      "provenance",
      "candidateHash",
      "state",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "wisdom-weasel.lesson-candidate-projection.v1"
      },
      "lessonCandidateId": {
        "type": "string"
      },
      "incidentId": {
        "type": "string"
      },
      "facts": {
        "type": "array"
      },
      "causes": {
        "type": "array"
      },
      "applicabilityBoundary": {
        "type": "object"
      },
      "counterexamples": {
        "type": "array"
      },
      "provenance": {
        "type": "array"
      },
      "candidateHash": {
        "type": "string"
      },
      "state": {
        "const": "candidate_only"
      },
      "createdAtMs": {
        "type": "integer"
      }
    }
  },
  "management-work-error.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.management-work-error.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "ok",
      "errorCode",
      "error",
      "currentRevision"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.management-work-error.v1"
      },
      "ok": {
        "type": "boolean",
        "const": false
      },
      "errorCode": {
        "type": "string",
        "enum": [
          "confirmation_mismatch",
          "domain_not_applicable",
          "domain_not_found",
          "domain_rejected",
          "domain_scope_mismatch",
          "invalid_payload",
          "invalid_request",
          "payload_hash_mismatch",
          "payload_too_large",
          "preview_already_used",
          "preview_expired",
          "preview_not_found",
          "preview_path_mismatch",
          "receipt_already_rolled_back",
          "receipt_not_found",
          "receipt_path_mismatch",
          "revision_mismatch",
          "rollback_authority_mismatch",
          "rollback_path_mismatch",
          "rollback_state_changed",
          "rollback_token_mismatch",
          "rollback_unavailable",
          "stored_contract_invalid",
          "unsupported_backend",
          "unsupported_mutation"
        ]
      },
      "error": {
        "type": "string",
        "minLength": 1
      },
      "currentRevision": {
        "type": "object"
      }
    }
  },
  "management-work-preview.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.management-work-preview.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "ok",
      "previewToken",
      "pathId",
      "payloadSha256",
      "expectedRevision",
      "expiresAtMs",
      "requiredConfirm",
      "summary"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.management-work-preview.v1"
      },
      "ok": {
        "type": "boolean",
        "const": true
      },
      "previewToken": {
        "type": "string",
        "minLength": 32
      },
      "pathId": {
        "type": "string",
        "minLength": 1
      },
      "payloadSha256": {
        "type": "string",
        "minLength": 71
      },
      "expectedRevision": {
        "type": "object"
      },
      "expiresAtMs": {
        "type": "integer",
        "minimum": 1
      },
      "requiredConfirm": {
        "type": "string",
        "minLength": 1
      },
      "summary": {
        "type": "object",
        "required": [
          "title",
          "items",
          "risk"
        ],
        "properties": {
          "title": {
            "type": "string",
            "minLength": 1
          },
          "items": {
            "type": "array",
            "items": {
              "type": "string"
            }
          },
          "risk": {
            "type": "string",
            "enum": [
              "R1",
              "R2",
              "R3"
            ]
          }
        }
      }
    }
  },
  "management-work-receipt.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.management-work-receipt.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "ok",
      "receiptId",
      "pathId",
      "payloadSha256",
      "appliedAtMs",
      "auditId",
      "rollbackAvailable",
      "rollbackToken",
      "rollbackAuthority",
      "restartComponents",
      "result"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.management-work-receipt.v1"
      },
      "ok": {
        "type": "boolean",
        "const": true
      },
      "receiptId": {
        "type": "string",
        "minLength": 1
      },
      "pathId": {
        "type": "string",
        "minLength": 1
      },
      "payloadSha256": {
        "type": "string",
        "minLength": 71
      },
      "appliedAtMs": {
        "type": "integer",
        "minimum": 1
      },
      "auditId": {
        "type": "integer",
        "minimum": 1
      },
      "rollbackAvailable": {
        "type": "boolean"
      },
      "rollbackToken": {
        "type": "string"
      },
      "rollbackAuthority": {
        "type": "object"
      },
      "restartComponents": {
        "type": "array",
        "items": {
          "type": "string"
        }
      },
      "result": {
        "type": "object"
      },
      "rollbackOfReceiptId": {
        "type": "string",
        "minLength": 1
      }
    }
  },
  "memory-bootstrap.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.memory-bootstrap.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "bootstrapId",
      "sessionId",
      "project",
      "roleId",
      "generatedAtMs",
      "queryFree",
      "sections",
      "sourceIds",
      "budget",
      "policy"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.memory-bootstrap.v1"
      },
      "bootstrapId": {
        "type": "string",
        "minLength": 1
      },
      "sessionId": {
        "type": "string",
        "minLength": 1
      },
      "project": {
        "type": "string"
      },
      "roleId": {
        "type": "string"
      },
      "generatedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "queryFree": {
        "type": "boolean",
        "const": true
      },
      "sections": {
        "type": "object",
        "required": [
          "stablePreferences",
          "projectState",
          "topicBooks",
          "recentTimeline",
          "activeAtoms",
          "oneRing"
        ],
        "properties": {
          "stablePreferences": {
            "type": "array",
            "items": {
              "$ref": "#/$defs/contextSource"
            }
          },
          "projectState": {
            "type": "array",
            "items": {
              "$ref": "#/$defs/contextSource"
            }
          },
          "topicBooks": {
            "type": "array",
            "items": {
              "$ref": "#/$defs/contextSource"
            }
          },
          "recentTimeline": {
            "type": "array",
            "items": {
              "$ref": "#/$defs/contextSource"
            }
          },
          "activeAtoms": {
            "type": "array",
            "items": {
              "$ref": "#/$defs/contextSource"
            }
          },
          "oneRing": {
            "type": "array",
            "items": {
              "$ref": "#/$defs/contextSource"
            }
          }
        }
      },
      "sourceIds": {
        "type": "array",
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "budget": {
        "type": "object",
        "required": [
          "maxChars",
          "usedChars",
          "omittedCounts"
        ],
        "properties": {
          "maxChars": {
            "type": "integer",
            "minimum": 1
          },
          "usedChars": {
            "type": "integer",
            "minimum": 0
          },
          "omittedCounts": {
            "type": "object"
          }
        }
      },
      "policy": {
        "type": "object",
        "required": [
          "automaticRecall",
          "lifecycle",
          "oneRingMaySupportFacts",
          "rawDialogueIsLongTermFact"
        ],
        "properties": {
          "automaticRecall": {
            "type": "string",
            "const": "session_start_only"
          },
          "lifecycle": {
            "type": "string",
            "const": "once"
          },
          "oneRingMaySupportFacts": {
            "type": "boolean",
            "const": false
          },
          "rawDialogueIsLongTermFact": {
            "type": "boolean",
            "const": false
          },
          "layerBoundaries": {
            "type": "object",
            "required": [
              "evidence",
              "atom",
              "topicBook",
              "roleBook",
              "timeline"
            ],
            "properties": {
              "evidence": {
                "type": "string"
              },
              "atom": {
                "type": "string"
              },
              "topicBook": {
                "type": "string"
              },
              "roleBook": {
                "type": "string"
              },
              "timeline": {
                "type": "string"
              }
            }
          }
        }
      }
    },
    "$defs": {
      "contextSource": {
        "type": "object",
        "required": [
          "sourceType",
          "sourceId",
          "text",
          "occurredAtMs",
          "maySupportFacts",
          "provenance"
        ],
        "properties": {
          "sourceType": {
            "type": "string",
            "enum": [
              "memory_atom",
              "memory_book",
              "memory_evidence",
              "planning_item"
            ]
          },
          "sourceId": {
            "type": "string",
            "minLength": 1
          },
          "text": {
            "type": "string",
            "minLength": 1
          },
          "kind": {
            "type": "string"
          },
          "occurredAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "maySupportFacts": {
            "type": "boolean"
          },
          "provenance": {
            "type": "object"
          },
          "source": {
            "$ref": "#/$defs/sourceRef"
          },
          "ref": {
            "$ref": "#/$defs/sourceRef"
          }
        }
      },
      "sourceRef": {
        "type": "object",
        "required": [
          "type",
          "id"
        ],
        "properties": {
          "type": {
            "type": "string"
          },
          "id": {
            "type": "string"
          },
          "bookId": {
            "type": "string"
          }
        }
      }
    }
  },
  "memory-catalog.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.memory-catalog.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "project",
      "catalogVersion",
      "items"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.memory-catalog.v1"
      },
      "project": {
        "type": "string",
        "minLength": 1
      },
      "catalogVersion": {
        "type": "string",
        "minLength": 1
      },
      "items": {
        "type": "array",
        "items": {
          "$ref": "#/$defs/item"
        }
      }
    },
    "$defs": {
      "item": {
        "type": "object",
        "required": [
          "bookId",
          "bookKey",
          "bookType",
          "title",
          "summary",
          "updatedAtMs",
          "sourceCount",
          "status"
        ],
        "properties": {
          "bookId": {
            "type": "string",
            "minLength": 1
          },
          "bookKey": {
            "type": "string",
            "minLength": 1
          },
          "bookType": {
            "type": "string",
            "enum": [
              "daily",
              "topic",
              "project",
              "session"
            ]
          },
          "title": {
            "type": "string",
            "minLength": 1
          },
          "summary": {
            "type": "string"
          },
          "updatedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "sourceCount": {
            "type": "integer",
            "minimum": 0
          },
          "status": {
            "type": "string",
            "enum": [
              "active",
              "archived"
            ]
          }
        }
      }
    }
  },
  "memory-entity.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.memory-entity.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "ok",
      "settingsRevision",
      "runtimeRevision",
      "kind",
      "entityId",
      "entityRevision",
      "project",
      "entity",
      "attributes",
      "connections",
      "members",
      "limits"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.memory-entity.v1"
      },
      "ok": {
        "type": "boolean",
        "const": true
      },
      "settingsRevision": {
        "type": "string",
        "minLength": 1
      },
      "runtimeRevision": {
        "type": "integer",
        "minimum": 0
      },
      "kind": {
        "type": "string",
        "enum": [
          "tag",
          "group",
          "book"
        ]
      },
      "entityId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 128
      },
      "entityRevision": {
        "type": "string",
        "pattern": "^sha256:[0-9a-f]{64}$"
      },
      "project": {
        "type": "string",
        "maxLength": 128
      },
      "entity": {
        "$ref": "#/$defs/node"
      },
      "topicPage": {
        "$ref": "#/$defs/topicPage"
      },
      "attributes": {
        "$ref": "#/$defs/attributes"
      },
      "connections": {
        "$ref": "#/$defs/page"
      },
      "members": {
        "$ref": "#/$defs/page"
      },
      "limits": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "connectionsLimit",
          "membersLimit"
        ],
        "properties": {
          "connectionsLimit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100
          },
          "membersLimit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100
          }
        }
      }
    },
    "$defs": {
      "topicReference": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "kind",
          "id",
          "referenceKind",
          "referenceId"
        ],
        "properties": {
          "kind": {
            "type": "string",
            "enum": [
              "atom",
              "evidence",
              "event"
            ]
          },
          "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "referenceKind": {
            "type": "string",
            "enum": [
              "atom",
              "evidence",
              "event"
            ]
          },
          "referenceId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "label": {
            "type": "string",
            "maxLength": 180
          }
        }
      },
      "topicEntry": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "id",
          "text",
          "kind",
          "status",
          "claimState",
          "atomIds",
          "references",
          "sourceStatus",
          "lineageId",
          "validFromMs",
          "validToMs",
          "supersedesId",
          "supersededByIds",
          "reason"
        ],
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "text": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4000
          },
          "kind": {
            "type": "string",
            "maxLength": 64
          },
          "status": {
            "type": "string",
            "maxLength": 32
          },
          "claimState": {
            "type": "string",
            "enum": [
              "current",
              "superseded"
            ]
          },
          "atomIds": {
            "type": "array",
            "maxItems": 1,
            "minItems": 1,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 240
            }
          },
          "references": {
            "type": "array",
            "maxItems": 21,
            "items": {
              "$ref": "#/$defs/topicReference"
            }
          },
          "sourceStatus": {
            "type": "string",
            "enum": [
              "available",
              "unavailable"
            ]
          },
          "lineageId": {
            "type": "string",
            "maxLength": 240
          },
          "validFromMs": {
            "type": "integer",
            "minimum": 0
          },
          "validToMs": {
            "type": [
              "integer",
              "null"
            ],
            "minimum": 0
          },
          "supersedesId": {
            "type": "string",
            "maxLength": 240
          },
          "supersededByIds": {
            "type": "array",
            "maxItems": 256,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 240
            }
          },
          "reason": {
            "type": "null"
          }
        }
      },
      "topicPage": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "schemaVersion",
          "bookId",
          "revision",
          "authority",
          "freshness",
          "summary",
          "sections",
          "sources",
          "coverage"
        ],
        "properties": {
          "schemaVersion": {
            "type": "string",
            "const": "rag-ime.memory-topic-page.v1"
          },
          "bookId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128
          },
          "revision": {
            "type": "string",
            "pattern": "^sha256:[0-9a-f]{64}$"
          },
          "authority": {
            "type": "string",
            "const": "atom_projection"
          },
          "freshness": {
            "type": "string",
            "enum": [
              "current",
              "needs_refresh"
            ]
          },
          "summary": {
            "type": "string",
            "maxLength": 900
          },
          "sections": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "current",
              "constraints",
              "openQuestions",
              "history"
            ],
            "properties": {
              "current": {
                "type": "array",
                "maxItems": 256,
                "items": {
                  "$ref": "#/$defs/topicEntry"
                }
              },
              "constraints": {
                "type": "array",
                "maxItems": 256,
                "items": {
                  "$ref": "#/$defs/topicEntry"
                }
              },
              "openQuestions": {
                "type": "array",
                "maxItems": 256,
                "items": {
                  "$ref": "#/$defs/topicEntry"
                }
              },
              "history": {
                "type": "array",
                "maxItems": 256,
                "items": {
                  "$ref": "#/$defs/topicEntry"
                }
              }
            }
          },
          "sources": {
            "type": "array",
            "maxItems": 80,
            "items": {
              "$ref": "#/$defs/topicReference"
            }
          },
          "coverage": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "memberCount",
              "visibleAtomCount",
              "omittedAtomCount",
              "truncated"
            ],
            "properties": {
              "memberCount": {
                "type": "integer",
                "minimum": 0
              },
              "visibleAtomCount": {
                "type": "integer",
                "minimum": 0
              },
              "omittedAtomCount": {
                "type": "integer",
                "minimum": 0
              },
              "truncated": {
                "type": "boolean"
              }
            }
          }
        }
      },
      "nodeKind": {
        "type": "string",
        "enum": [
          "tag",
          "group",
          "atom",
          "book",
          "phrase",
          "memory"
        ]
      },
      "node": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "id",
          "entityId",
          "kind",
          "label",
          "description",
          "color",
          "status",
          "source",
          "project",
          "qualityScore",
          "memberCount",
          "edgeCount",
          "updatedAtMs"
        ],
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "entityId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128
          },
          "kind": {
            "$ref": "#/$defs/nodeKind"
          },
          "label": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120
          },
          "description": {
            "type": "string",
            "maxLength": 240
          },
          "color": {
            "type": "string",
            "enum": [
              "blue",
              "teal",
              "green",
              "orange",
              "pink",
              "purple",
              "gray"
            ]
          },
          "status": {
            "type": "string",
            "minLength": 1,
            "maxLength": 32
          },
          "source": {
            "type": "string",
            "maxLength": 64
          },
          "project": {
            "type": "string",
            "maxLength": 128
          },
          "qualityScore": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          },
          "memberCount": {
            "type": "integer",
            "minimum": 0
          },
          "edgeCount": {
            "type": "integer",
            "minimum": 0
          },
          "updatedAtMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "attributes": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "type",
          "aliases",
          "tags"
        ],
        "properties": {
          "type": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64
          },
          "aliases": {
            "type": "array",
            "maxItems": 64,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 64
            }
          },
          "tags": {
            "type": "array",
            "maxItems": 64,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 64
            }
          }
        }
      },
      "edge": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "id",
          "kind",
          "sourceId",
          "targetId",
          "sourceKind",
          "targetKind",
          "relation",
          "weight",
          "directionBias",
          "evidenceCount",
          "source",
          "updatedAtMs"
        ],
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 512
          },
          "kind": {
            "type": "string",
            "enum": [
              "tagRelation",
              "groupMember",
              "tagMember"
            ]
          },
          "sourceId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "targetId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "sourceKind": {
            "$ref": "#/$defs/nodeKind"
          },
          "targetKind": {
            "$ref": "#/$defs/nodeKind"
          },
          "relation": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64
          },
          "weight": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          },
          "directionBias": {
            "type": "number",
            "minimum": -1,
            "maximum": 1
          },
          "evidenceCount": {
            "type": "integer",
            "minimum": 0
          },
          "source": {
            "type": "string",
            "maxLength": 64
          },
          "updatedAtMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "related": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "node",
          "edge"
        ],
        "properties": {
          "node": {
            "$ref": "#/$defs/node"
          },
          "edge": {
            "$ref": "#/$defs/edge"
          }
        }
      },
      "page": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "items",
          "nextCursor",
          "limit",
          "hasMore"
        ],
        "properties": {
          "items": {
            "type": "array",
            "maxItems": 100,
            "items": {
              "$ref": "#/$defs/related"
            }
          },
          "nextCursor": {
            "type": "string",
            "maxLength": 7
          },
          "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100
          },
          "hasMore": {
            "type": "boolean"
          }
        }
      }
    }
  },
  "memory-governance-preview.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.memory-governance-preview.v1",
    "title": "Memory Governance Preview",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "previewId",
      "proposalId",
      "operation",
      "applyOperation",
      "sessionId",
      "project",
      "targetId",
      "memoryKind",
      "proposedText",
      "reason",
      "evidenceIds",
      "summary",
      "status",
      "reviewRequired",
      "applyOperationAvailable",
      "mutationApplied",
      "writes",
      "audit",
      "createdAtMs",
      "expiresAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.memory-governance-preview.v1"
      },
      "previewId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "proposalId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "operation": {
        "type": "string",
        "enum": [
          "remember_preview",
          "correct_preview",
          "forget_preview"
        ]
      },
      "applyOperation": {
        "type": "string",
        "enum": [
          "remember_apply",
          "correct_apply",
          "forget_apply"
        ]
      },
      "sessionId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "project": {
        "type": "string",
        "maxLength": 200
      },
      "targetId": {
        "type": "string",
        "maxLength": 240
      },
      "memoryKind": {
        "type": "string",
        "enum": [
          "",
          "fact",
          "preference",
          "decision",
          "commitment",
          "project_state"
        ]
      },
      "proposedText": {
        "type": "string",
        "maxLength": 1200
      },
      "reason": {
        "type": "string",
        "maxLength": 400
      },
      "evidenceIds": {
        "type": "array",
        "minItems": 1,
        "maxItems": 16,
        "items": {
          "type": "string",
          "minLength": 1,
          "maxLength": 240
        }
      },
      "summary": {
        "type": "string",
        "minLength": 1,
        "maxLength": 400
      },
      "status": {
        "type": "string",
        "enum": [
          "ready",
          "applied",
          "rolled_back",
          "expired",
          "failed"
        ]
      },
      "reviewRequired": {
        "type": "boolean",
        "const": true
      },
      "applyOperationAvailable": {
        "type": "boolean"
      },
      "mutationApplied": {
        "type": "boolean",
        "const": false
      },
      "writes": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "proposalStored",
          "memoryAtoms",
          "memoryBooks",
          "retrievalVectors"
        ],
        "properties": {
          "proposalStored": {
            "type": "boolean",
            "const": true
          },
          "memoryAtoms": {
            "type": "boolean",
            "const": false
          },
          "memoryBooks": {
            "type": "boolean",
            "const": false
          },
          "retrievalVectors": {
            "type": "boolean",
            "const": false
          }
        }
      },
      "audit": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "payloadSha256",
          "recordKind",
          "sessionId",
          "idempotencyKey"
        ],
        "properties": {
          "payloadSha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64
          },
          "recordKind": {
            "type": "string",
            "const": "memory_governance_proposal"
          },
          "sessionId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "idempotencyKey": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          }
        }
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "expiresAtMs": {
        "type": "integer",
        "minimum": 0
      }
    }
  },
  "memory-graph.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.memory-graph.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "ok",
      "settingsRevision",
      "runtimeRevision",
      "graphRevision",
      "plane",
      "project",
      "filters",
      "nodes",
      "edges",
      "truncated",
      "limits"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.memory-graph.v1"
      },
      "ok": {
        "type": "boolean",
        "const": true
      },
      "settingsRevision": {
        "type": "string",
        "minLength": 1
      },
      "runtimeRevision": {
        "type": "integer",
        "minimum": 0
      },
      "graphRevision": {
        "type": "string",
        "pattern": "^sha256:[0-9a-f]{64}$"
      },
      "plane": {
        "type": "string",
        "enum": [
          "tags",
          "groups"
        ]
      },
      "project": {
        "type": "string",
        "maxLength": 128
      },
      "filters": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "status",
          "query",
          "focusId",
          "minWeight"
        ],
        "properties": {
          "status": {
            "type": "string",
            "enum": [
              "active",
              "merged",
              "all"
            ]
          },
          "query": {
            "type": "string",
            "maxLength": 200
          },
          "focusId": {
            "type": "string",
            "maxLength": 128
          },
          "minWeight": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          }
        }
      },
      "nodes": {
        "type": "array",
        "maxItems": 200,
        "items": {
          "$ref": "#/$defs/node"
        }
      },
      "edges": {
        "type": "array",
        "maxItems": 500,
        "items": {
          "$ref": "#/$defs/edge"
        }
      },
      "truncated": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "nodes",
          "edges"
        ],
        "properties": {
          "nodes": {
            "type": "boolean"
          },
          "edges": {
            "type": "boolean"
          }
        }
      },
      "limits": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "nodeLimit",
          "edgeLimit",
          "depth"
        ],
        "properties": {
          "nodeLimit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 200
          },
          "edgeLimit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500
          },
          "depth": {
            "type": "integer",
            "minimum": 0,
            "maximum": 2
          }
        }
      }
    },
    "$defs": {
      "nodeKind": {
        "type": "string",
        "enum": [
          "tag",
          "group",
          "atom",
          "book",
          "phrase"
        ]
      },
      "node": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "id",
          "entityId",
          "kind",
          "label",
          "description",
          "color",
          "status",
          "source",
          "project",
          "qualityScore",
          "memberCount",
          "edgeCount",
          "updatedAtMs"
        ],
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "entityId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128
          },
          "kind": {
            "$ref": "#/$defs/nodeKind"
          },
          "label": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120
          },
          "description": {
            "type": "string",
            "maxLength": 240
          },
          "color": {
            "type": "string",
            "enum": [
              "blue",
              "teal",
              "green",
              "orange",
              "pink",
              "purple",
              "gray"
            ]
          },
          "status": {
            "type": "string",
            "minLength": 1,
            "maxLength": 32
          },
          "source": {
            "type": "string",
            "maxLength": 64
          },
          "project": {
            "type": "string",
            "maxLength": 128
          },
          "qualityScore": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          },
          "memberCount": {
            "type": "integer",
            "minimum": 0
          },
          "edgeCount": {
            "type": "integer",
            "minimum": 0
          },
          "updatedAtMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "edge": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "id",
          "kind",
          "sourceId",
          "targetId",
          "sourceKind",
          "targetKind",
          "relation",
          "weight",
          "directionBias",
          "evidenceCount",
          "source",
          "updatedAtMs"
        ],
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 512
          },
          "kind": {
            "type": "string",
            "enum": [
              "tagRelation",
              "groupMember"
            ]
          },
          "sourceId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "targetId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "sourceKind": {
            "$ref": "#/$defs/nodeKind"
          },
          "targetKind": {
            "$ref": "#/$defs/nodeKind"
          },
          "relation": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64
          },
          "weight": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          },
          "directionBias": {
            "type": "number",
            "minimum": -1,
            "maximum": 1
          },
          "evidenceCount": {
            "type": "integer",
            "minimum": 0
          },
          "source": {
            "type": "string",
            "maxLength": 64
          },
          "updatedAtMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      }
    }
  },
  "memory-read-error.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.memory-read-error.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "ok",
      "errorCode",
      "error"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.memory-read-error.v1"
      },
      "ok": {
        "type": "boolean",
        "const": false
      },
      "errorCode": {
        "type": "string",
        "const": "invalid_request"
      },
      "error": {
        "type": "string",
        "minLength": 1,
        "maxLength": 256
      }
    }
  },
  "memory-reference.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.memory-reference.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "settingsRevision",
      "runtimeRevision",
      "ok",
      "kind",
      "referenceId",
      "item",
      "source",
      "ref",
      "evidenceRefs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.memory-reference.v1"
      },
      "settingsRevision": {
        "type": "string",
        "minLength": 1
      },
      "runtimeRevision": {
        "type": "integer",
        "minimum": 0
      },
      "ok": {
        "type": "boolean",
        "const": true
      },
      "kind": {
        "$ref": "#/$defs/referenceKind"
      },
      "referenceId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "item": {
        "type": "object",
        "required": [
          "id",
          "title",
          "status"
        ],
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "title": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "status": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64
          },
          "text": {
            "type": "string",
            "maxLength": 32000
          },
          "textPreview": {
            "type": "string",
            "maxLength": 2000
          },
          "summary": {
            "type": "string",
            "maxLength": 2000
          },
          "detail": {
            "type": "string",
            "maxLength": 2000
          },
          "sensitive": {
            "type": "boolean"
          },
          "ownerKind": {
            "type": "string",
            "maxLength": 32
          },
          "ownerId": {
            "type": "string",
            "maxLength": 240
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "updatedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "occurredAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "sourceContextAvailable": {
            "type": "boolean"
          },
          "sourceContext": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "recentContext",
              "preedit",
              "redacted",
              "scopeProject",
              "usedFor"
            ],
            "properties": {
              "recentContext": {
                "type": "string",
                "maxLength": 32000
              },
              "preedit": {
                "type": "string",
                "maxLength": 32000
              },
              "redacted": {
                "type": "boolean"
              },
              "scopeProject": {
                "type": "string"
              },
              "usedFor": {
                "type": "array",
                "items": {
                  "type": "string",
                  "enum": [
                    "source_fingerprint",
                    "semantic_grouping"
                  ]
                },
                "uniqueItems": true
              }
            }
          }
        }
      },
      "source": {
        "$ref": "#/$defs/source"
      },
      "ref": {
        "$ref": "#/$defs/reference"
      },
      "evidenceRefs": {
        "type": "array",
        "maxItems": 80,
        "items": {
          "$ref": "#/$defs/reference"
        }
      }
    },
    "$defs": {
      "referenceKind": {
        "type": "string",
        "enum": [
          "event",
          "evidence",
          "atom",
          "book",
          "timeline",
          "role_book_revision"
        ]
      },
      "source": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "kind",
          "id"
        ],
        "properties": {
          "kind": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80
          },
          "sourceKind": {
            "type": "string",
            "maxLength": 120
          },
          "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 320
          }
        }
      },
      "reference": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "kind",
          "id",
          "referenceKind",
          "referenceId"
        ],
        "properties": {
          "kind": {
            "$ref": "#/$defs/referenceKind"
          },
          "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "referenceKind": {
            "$ref": "#/$defs/referenceKind"
          },
          "referenceId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "label": {
            "type": "string",
            "maxLength": 180
          }
        }
      }
    }
  },
  "observability-eval-list.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.observability-eval-list.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "traceId",
      "total",
      "truncated",
      "items"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.observability-eval-list.v1"
      },
      "traceId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 128
      },
      "total": {
        "type": "integer",
        "minimum": 0
      },
      "truncated": {
        "type": "boolean"
      },
      "items": {
        "type": "array",
        "maxItems": 500,
        "items": {
          "$ref": "#/$defs/evalSummary"
        }
      }
    },
    "$defs": {
      "evalSummary": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "evalRunId",
          "mode",
          "metricAuthority",
          "truthStatus",
          "datasetId",
          "labelRevision",
          "evaluatorDisplayName",
          "metrics",
          "status",
          "createdAtMs",
          "updatedAtMs"
        ],
        "properties": {
          "evalRunId": {
            "type": "string",
            "minLength": 1
          },
          "mode": {
            "enum": [
              "ground_truth",
              "ai_judge"
            ]
          },
          "metricAuthority": {
            "enum": [
              "ground_truth",
              "ai_judge_estimate"
            ]
          },
          "truthStatus": {
            "enum": [
              "none",
              "human",
              "frozen"
            ]
          },
          "datasetId": {
            "type": "string"
          },
          "labelRevision": {
            "type": "string"
          },
          "evaluatorDisplayName": {
            "type": "string"
          },
          "suiteBinding": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "suiteId",
              "suiteRevision"
            ],
            "properties": {
              "suiteId": {
                "type": "string",
                "minLength": 1
              },
              "suiteRevision": {
                "type": "string",
                "minLength": 1
              }
            }
          },
          "metrics": {
            "type": "object",
            "additionalProperties": {
              "type": "number"
            }
          },
          "status": {
            "enum": [
              "queued",
              "running",
              "completed",
              "failed"
            ]
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "updatedAtMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      }
    }
  },
  "observability-evidence-eval-request.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.observability-evidence-eval-request.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "traceId",
      "requiredEvidenceIds",
      "datasetId",
      "labelRevision",
      "truthKind"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.observability-evidence-eval-request.v1"
      },
      "traceId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$"
      },
      "requiredEvidenceIds": {
        "type": "array",
        "maxItems": 2048,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "minLength": 1,
          "maxLength": 512
        }
      },
      "datasetId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "labelRevision": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "truthKind": {
        "const": "human"
      }
    }
  },
  "observability-sandbox-run-list.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.observability-sandbox-run-list.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "ok",
      "items",
      "total"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.observability-sandbox-run-list.v1"
      },
      "ok": {
        "const": true
      },
      "items": {
        "type": "array",
        "maxItems": 500,
        "items": {
          "$ref": "#/$defs/sandboxRun"
        }
      },
      "total": {
        "type": "integer",
        "minimum": 0
      }
    },
    "$defs": {
      "sandboxRun": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "schemaVersion",
          "sandboxRunId",
          "appId",
          "status",
          "policy",
          "traceIds",
          "evalRunIds",
          "createdAtMs",
          "updatedAtMs"
        ],
        "properties": {
          "schemaVersion": {
            "const": "rag-ime.sandbox-run.v1"
          },
          "sandboxRunId": {
            "type": "string",
            "minLength": 1
          },
          "appId": {
            "type": "string",
            "minLength": 1
          },
          "status": {
            "enum": [
              "queued",
              "running",
              "completed",
              "failed",
              "cancelled"
            ]
          },
          "policy": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "workspaceBindingId",
              "workspaceFingerprint",
              "mutationMode",
              "network",
              "productionWriteBlocked"
            ],
            "properties": {
              "workspaceBindingId": {
                "type": "string",
                "minLength": 1
              },
              "workspaceFingerprint": {
                "type": "string",
                "pattern": "^sha256:[a-f0-9]{64}$"
              },
              "mutationMode": {
                "enum": [
                  "read_only",
                  "staged"
                ]
              },
              "network": {
                "enum": [
                  "blocked",
                  "allowlisted"
                ]
              },
              "productionWriteBlocked": {
                "const": true
              }
            }
          },
          "traceIds": {
            "type": "array",
            "uniqueItems": true,
            "items": {
              "type": "string",
              "minLength": 1
            }
          },
          "evalRunIds": {
            "type": "array",
            "uniqueItems": true,
            "items": {
              "type": "string",
              "minLength": 1
            }
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "updatedAtMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      }
    }
  },
  "observability-trace-error.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.observability-trace-error.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "ok",
      "errorCode",
      "error",
      "traceId"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.observability-trace-error.v1"
      },
      "ok": {
        "const": false
      },
      "errorCode": {
        "enum": [
          "trace_not_found",
          "invalid_trace_id",
          "trace_invalid"
        ]
      },
      "error": {
        "type": "string",
        "minLength": 1
      },
      "traceId": {
        "type": "string"
      }
    }
  },
  "observability-trace-get.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.observability-trace-get.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "traceId",
      "trace",
      "truncated",
      "projectionSource",
      "observationWindow"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.observability-trace-get.v1"
      },
      "traceId": {
        "type": "string",
        "minLength": 1
      },
      "trace": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "schemaVersion",
          "traceId",
          "sourceKind",
          "status",
          "binding",
          "input",
          "spans",
          "evidence",
          "artifacts",
          "createdAtMs",
          "updatedAtMs"
        ],
        "properties": {
          "schemaVersion": {
            "const": "rag-ime.trace-envelope.v1"
          },
          "traceId": {
            "type": "string",
            "minLength": 1
          },
          "sourceKind": {
            "type": "string",
            "minLength": 1
          },
          "status": {
            "enum": [
              "building",
              "completed",
              "failed",
              "cancelled"
            ]
          },
          "binding": {
            "type": "object",
            "additionalProperties": false,
            "properties": {
              "sessionId": {
                "type": "string"
              },
              "turnId": {
                "type": "string"
              },
              "roomId": {
                "type": "string"
              },
              "runId": {
                "type": "string"
              },
              "sourceLoopId": {
                "type": "string"
              },
              "workItemId": {
                "type": "string"
              },
              "caseId": {
                "type": "string"
              }
            }
          },
          "parentTraceId": {
            "type": [
              "string",
              "null"
            ],
            "minLength": 1,
            "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$"
          },
          "links": {
            "type": "array",
            "maxItems": 64,
            "items": {
              "$ref": "#/$defs/traceLink"
            }
          },
          "input": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "fingerprint",
              "contentPolicy",
              "normalization"
            ],
            "properties": {
              "fingerprint": {
                "type": "string",
                "pattern": "^sha256:[a-f0-9]{64}$"
              },
              "contentPolicy": {
                "enum": [
                  "hash_only",
                  "redacted",
                  "owner_local"
                ]
              },
              "normalization": {
                "type": "string",
                "minLength": 1
              }
            }
          },
          "spans": {
            "type": "array",
            "maxItems": 256,
            "items": {
              "$ref": "#/$defs/span"
            }
          },
          "evidence": {
            "type": "array",
            "maxItems": 2048,
            "items": {
              "$ref": "#/$defs/evidence"
            }
          },
          "artifacts": {
            "type": "array",
            "maxItems": 256,
            "items": {
              "$ref": "#/$defs/artifact"
            }
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "updatedAtMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "truncated": {
        "type": "boolean"
      },
      "projectionSource": {
        "enum": [
          "observation_journal",
          "source_adapter",
          "trace_store"
        ]
      },
      "observationWindow": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "firstSequence",
          "lastSequence",
          "resumeToken",
          "nextBeforeSequence"
        ],
        "properties": {
          "firstSequence": {
            "type": "integer",
            "minimum": 0
          },
          "lastSequence": {
            "type": "integer",
            "minimum": 0
          },
          "resumeToken": {
            "type": "string",
            "minLength": 1
          },
          "nextBeforeSequence": {
            "type": [
              "integer",
              "null"
            ],
            "minimum": 0
          }
        }
      }
    },
    "$defs": {
      "optimizationExecution": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "candidateId",
          "role",
          "comparisonContractSha256",
          "controls",
          "loadedVersions"
        ],
        "properties": {
          "candidateId": {
            "type": "string",
            "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$"
          },
          "role": {
            "enum": [
              "baseline",
              "candidate"
            ]
          },
          "comparisonContractSha256": {
            "type": "string",
            "pattern": "^[a-f0-9]{64}$"
          },
          "controls": {
            "type": "object",
            "maxProperties": 32,
            "propertyNames": {
              "type": "string",
              "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$"
            },
            "additionalProperties": {
              "type": "string",
              "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$"
            },
            "required": [
              "inputState",
              "evaluator",
              "qualityPolicy",
              "permissions",
              "environment"
            ]
          },
          "loadedVersions": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "tool",
              "skill",
              "prompt",
              "workflow",
              "model"
            ],
            "properties": {
              "tool": {
                "type": "string",
                "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$"
              },
              "skill": {
                "type": "string",
                "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$"
              },
              "prompt": {
                "type": "string",
                "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$"
              },
              "workflow": {
                "type": "string",
                "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$"
              },
              "model": {
                "type": "string",
                "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$"
              }
            }
          },
          "fixedContextFingerprints": {
            "type": "object",
            "maxProperties": 32,
            "propertyNames": {
              "type": "string",
              "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$"
            },
            "additionalProperties": {
              "type": "string",
              "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$"
            }
          }
        }
      },
      "span": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "spanId",
          "name",
          "parentSpanId",
          "status",
          "startedAtMs",
          "endedAtMs",
          "durationMs",
          "recorded",
          "unavailableReason",
          "metrics",
          "attributes"
        ],
        "properties": {
          "spanId": {
            "type": "string",
            "minLength": 1
          },
          "name": {
            "type": "string",
            "minLength": 1
          },
          "parentSpanId": {
            "type": [
              "string",
              "null"
            ]
          },
          "status": {
            "enum": [
              "queued",
              "running",
              "waiting",
              "completed",
              "failed",
              "cancelled",
              "info"
            ]
          },
          "startedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "endedAtMs": {
            "type": [
              "integer",
              "null"
            ],
            "minimum": 0
          },
          "durationMs": {
            "type": [
              "integer",
              "null"
            ],
            "minimum": 0
          },
          "recorded": {
            "type": "boolean"
          },
          "unavailableReason": {
            "type": "string"
          },
          "metrics": {
            "type": "object"
          },
          "attributes": {
            "type": "object",
            "properties": {
              "traceOptimization": {
                "$ref": "#/$defs/optimizationExecution"
              }
            }
          }
        }
      },
      "evidence": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "evidenceId",
          "sourceKind",
          "sourceRef",
          "sourceLane",
          "evidenceStage",
          "disposition",
          "scores",
          "rankBefore",
          "rankAfter",
          "omissionReason"
        ],
        "properties": {
          "evidenceId": {
            "type": "string",
            "minLength": 1
          },
          "sourceKind": {
            "type": "string",
            "minLength": 1
          },
          "sourceRef": {
            "type": "string",
            "minLength": 1
          },
          "sourceLane": {
            "type": "string"
          },
          "evidenceStage": {
            "type": "string",
            "minLength": 1
          },
          "disposition": {
            "enum": [
              "included",
              "omitted",
              "filtered",
              "redacted"
            ]
          },
          "scores": {
            "type": "object",
            "additionalProperties": {
              "type": "number"
            }
          },
          "rankBefore": {
            "type": [
              "integer",
              "null"
            ],
            "minimum": 1
          },
          "rankAfter": {
            "type": [
              "integer",
              "null"
            ],
            "minimum": 1
          },
          "omissionReason": {
            "type": "string"
          }
        }
      },
      "artifact": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "artifactId",
          "kind",
          "mediaType",
          "sha256",
          "byteSize",
          "recordCount"
        ],
        "properties": {
          "artifactId": {
            "type": "string",
            "minLength": 1
          },
          "kind": {
            "type": "string",
            "minLength": 1
          },
          "mediaType": {
            "type": "string",
            "minLength": 1
          },
          "sha256": {
            "type": "string",
            "pattern": "^[a-f0-9]{64}$"
          },
          "byteSize": {
            "type": "integer",
            "minimum": 0
          },
          "recordCount": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "traceLink": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "traceId",
          "relation",
          "targetKind"
        ],
        "properties": {
          "traceId": {
            "type": "string",
            "minLength": 1,
            "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$"
          },
          "relation": {
            "enum": [
              "retry",
              "related"
            ]
          },
          "targetKind": {
            "const": "trace"
          }
        }
      }
    }
  },
  "observation-event.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.observation-event.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "eventType",
      "eventId",
      "sequence",
      "resumeToken",
      "traceId",
      "spanId",
      "parentSpanId",
      "sessionId",
      "roomId",
      "turnId",
      "runId",
      "category",
      "phase",
      "name",
      "status",
      "summary",
      "createdAtMs",
      "startedAtMs",
      "endedAtMs",
      "durationMs",
      "privacyClass",
      "metrics",
      "attributes",
      "refs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.observation-event.v1"
      },
      "eventType": {
        "type": "string",
        "enum": [
          "observation",
          "snapshot_required"
        ]
      },
      "eventId": {
        "type": "string",
        "minLength": 1
      },
      "sequence": {
        "type": "integer",
        "minimum": 0
      },
      "resumeToken": {
        "type": "string",
        "minLength": 1
      },
      "traceId": {
        "type": "string",
        "minLength": 1
      },
      "spanId": {
        "type": "string",
        "minLength": 1
      },
      "parentSpanId": {
        "type": "string"
      },
      "sessionId": {
        "type": "string"
      },
      "roomId": {
        "type": "string"
      },
      "turnId": {
        "type": "string"
      },
      "runId": {
        "type": "string"
      },
      "category": {
        "type": "string",
        "enum": [
          "context",
          "retrieval",
          "memory",
          "tool",
          "agent",
          "room",
          "intercom",
          "approval",
          "runtime",
          "system"
        ]
      },
      "phase": {
        "type": "string",
        "minLength": 1
      },
      "name": {
        "type": "string",
        "minLength": 1
      },
      "status": {
        "type": "string",
        "enum": [
          "queued",
          "running",
          "waiting",
          "completed",
          "failed",
          "cancelled",
          "expired",
          "info"
        ]
      },
      "summary": {
        "type": "string"
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "startedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "endedAtMs": {
        "type": [
          "integer",
          "null"
        ],
        "minimum": 0
      },
      "durationMs": {
        "type": [
          "number",
          "null"
        ],
        "minimum": 0
      },
      "privacyClass": {
        "type": "string",
        "enum": [
          "metadata",
          "redacted",
          "owner_local"
        ]
      },
      "metrics": {
        "type": "object"
      },
      "attributes": {
        "type": "object"
      },
      "refs": {
        "type": "array",
        "items": {
          "type": "object",
          "additionalProperties": false,
          "required": [
            "kind",
            "id",
            "label"
          ],
          "properties": {
            "kind": {
              "type": "string",
              "minLength": 1
            },
            "id": {
              "type": "string",
              "minLength": 1
            },
            "label": {
              "type": "string"
            }
          }
        }
      }
    }
  },
  "observation-snapshot.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.observation-snapshot.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "generatedAtMs",
      "firstSequence",
      "lastSequence",
      "resumeToken",
      "truncated",
      "filters",
      "counts",
      "items"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.observation-snapshot.v1"
      },
      "generatedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "firstSequence": {
        "type": "integer",
        "minimum": 0
      },
      "lastSequence": {
        "type": "integer",
        "minimum": 0
      },
      "resumeToken": {
        "type": "string",
        "minLength": 1
      },
      "truncated": {
        "type": "boolean"
      },
      "filters": {
        "type": "object"
      },
      "counts": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "total",
          "byCategory",
          "byStatus"
        ],
        "properties": {
          "total": {
            "type": "integer",
            "minimum": 0
          },
          "byCategory": {
            "type": "object"
          },
          "byStatus": {
            "type": "object"
          }
        }
      },
      "items": {
        "type": "array",
        "items": {
          "$ref": "#/$defs/event"
        }
      }
    },
    "$defs": {
      "event": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "schemaVersion",
          "eventType",
          "eventId",
          "sequence",
          "resumeToken",
          "traceId",
          "spanId",
          "parentSpanId",
          "sessionId",
          "roomId",
          "turnId",
          "runId",
          "category",
          "phase",
          "name",
          "status",
          "summary",
          "createdAtMs",
          "startedAtMs",
          "endedAtMs",
          "durationMs",
          "privacyClass",
          "metrics",
          "attributes",
          "refs"
        ],
        "properties": {
          "schemaVersion": {
            "type": "string",
            "const": "rag-ime.observation-event.v1"
          },
          "eventType": {
            "type": "string",
            "enum": [
              "observation",
              "snapshot_required"
            ]
          },
          "eventId": {
            "type": "string",
            "minLength": 1
          },
          "sequence": {
            "type": "integer",
            "minimum": 0
          },
          "resumeToken": {
            "type": "string",
            "minLength": 1
          },
          "traceId": {
            "type": "string",
            "minLength": 1
          },
          "spanId": {
            "type": "string",
            "minLength": 1
          },
          "parentSpanId": {
            "type": "string"
          },
          "sessionId": {
            "type": "string"
          },
          "roomId": {
            "type": "string"
          },
          "turnId": {
            "type": "string"
          },
          "runId": {
            "type": "string"
          },
          "category": {
            "type": "string",
            "enum": [
              "context",
              "retrieval",
              "memory",
              "tool",
              "agent",
              "room",
              "intercom",
              "approval",
              "runtime",
              "system"
            ]
          },
          "phase": {
            "type": "string",
            "minLength": 1
          },
          "name": {
            "type": "string",
            "minLength": 1
          },
          "status": {
            "type": "string",
            "enum": [
              "queued",
              "running",
              "waiting",
              "completed",
              "failed",
              "cancelled",
              "expired",
              "info"
            ]
          },
          "summary": {
            "type": "string"
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "startedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "endedAtMs": {
            "type": [
              "integer",
              "null"
            ],
            "minimum": 0
          },
          "durationMs": {
            "type": [
              "number",
              "null"
            ],
            "minimum": 0
          },
          "privacyClass": {
            "type": "string",
            "enum": [
              "metadata",
              "redacted",
              "owner_local"
            ]
          },
          "metrics": {
            "type": "object"
          },
          "attributes": {
            "type": "object"
          },
          "refs": {
            "type": "array",
            "items": {
              "type": "object",
              "additionalProperties": false,
              "required": [
                "kind",
                "id",
                "label"
              ],
              "properties": {
                "kind": {
                  "type": "string",
                  "minLength": 1
                },
                "id": {
                  "type": "string",
                  "minLength": 1
                },
                "label": {
                  "type": "string"
                }
              }
            }
          }
        }
      }
    }
  },
  "overlay-config.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.overlay-config.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "candidateFontSize",
      "maxWidth",
      "fadeAnimation",
      "panelStyle",
      "expiresAfterMs",
      "maxCandidates",
      "showSourceBadge",
      "badges",
      "colors",
      "keyPolicy",
      "activeRag"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.overlay-config.v1"
      },
      "candidateFontSize": {
        "type": "integer",
        "minimum": 11,
        "maximum": 24
      },
      "maxWidth": {
        "type": "integer",
        "minimum": 320,
        "maximum": 760
      },
      "fadeAnimation": {
        "type": "boolean"
      },
      "panelStyle": {
        "type": "string",
        "enum": [
          "compact",
          "expanded"
        ]
      },
      "expiresAfterMs": {
        "type": "integer",
        "minimum": 0,
        "maximum": 15000
      },
      "maxCandidates": {
        "type": "integer",
        "minimum": 1,
        "maximum": 10
      },
      "showSourceBadge": {
        "type": "boolean"
      },
      "badges": {
        "type": "object",
        "additionalProperties": {
          "type": "string"
        }
      },
      "colors": {
        "type": "object",
        "additionalProperties": {
          "type": "string"
        }
      },
      "keyPolicy": {
        "type": "object"
      },
      "activeRag": {
        "type": "object",
        "required": [
          "enabled",
          "shortcut"
        ],
        "properties": {
          "enabled": {
            "type": "boolean"
          },
          "shortcut": {
            "type": "string",
            "minLength": 1
          }
        }
      }
    }
  },
  "paw.plugin-usage-query.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "paw.plugin-usage-query.v1",
    "$defs": {
      "event": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "schemaVersion",
          "eventId",
          "occurredAtMs",
          "sessionId",
          "packageId",
          "packageVersion",
          "resourceKind",
          "resourceId",
          "activity"
        ],
        "properties": {
          "schemaVersion": {
            "const": "paw.plugin-usage.v1"
          },
          "eventId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "occurredAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "sessionId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "packageId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200
          },
          "packageVersion": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80
          },
          "resourceKind": {
            "enum": [
              "extension",
              "tool",
              "command",
              "skill",
              "prompt",
              "theme"
            ]
          },
          "resourceId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200
          },
          "activity": {
            "enum": [
              "loaded",
              "invoked",
              "finished"
            ]
          },
          "invocationId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "outcome": {
            "enum": [
              "succeeded",
              "failed",
              "cancelled"
            ]
          },
          "durationMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      }
    },
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "events",
      "aggregates"
    ],
    "properties": {
      "schemaVersion": {
        "const": "paw.plugin-usage-query.v1"
      },
      "events": {
        "type": "array",
        "items": {
          "$ref": "#/$defs/event"
        }
      },
      "aggregates": {
        "type": "array",
        "items": {
          "type": "object",
          "additionalProperties": false,
          "required": [
            "packageId",
            "packageVersion",
            "resourceKind",
            "resourceId",
            "loadedCount",
            "invocationCount",
            "terminalCount",
            "succeededCount",
            "failedCount",
            "cancelledCount",
            "averageDurationMs",
            "lastLoadedAtMs",
            "lastInvokedAtMs"
          ],
          "properties": {
            "packageId": {
              "type": "string",
              "minLength": 1,
              "maxLength": 200
            },
            "packageVersion": {
              "type": "string",
              "minLength": 1,
              "maxLength": 80
            },
            "resourceKind": {
              "enum": [
                "extension",
                "tool",
                "command",
                "skill",
                "prompt",
                "theme"
              ]
            },
            "resourceId": {
              "type": "string",
              "minLength": 1,
              "maxLength": 200
            },
            "loadedCount": {
              "type": "integer",
              "minimum": 0
            },
            "invocationCount": {
              "type": "integer",
              "minimum": 0
            },
            "terminalCount": {
              "type": "integer",
              "minimum": 0
            },
            "succeededCount": {
              "type": "integer",
              "minimum": 0
            },
            "failedCount": {
              "type": "integer",
              "minimum": 0
            },
            "cancelledCount": {
              "type": "integer",
              "minimum": 0
            },
            "averageDurationMs": {
              "type": [
                "integer",
                "null"
              ],
              "minimum": 0
            },
            "lastLoadedAtMs": {
              "type": [
                "integer",
                "null"
              ],
              "minimum": 0
            },
            "lastInvokedAtMs": {
              "type": [
                "integer",
                "null"
              ],
              "minimum": 0
            }
          }
        }
      }
    }
  },
  "paw.plugin-usage.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "paw.plugin-usage.v1",
    "title": "Privacy-preserving Pi Package resource usage event",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "eventId",
      "occurredAtMs",
      "sessionId",
      "packageId",
      "packageVersion",
      "resourceKind",
      "resourceId",
      "activity"
    ],
    "properties": {
      "schemaVersion": {
        "const": "paw.plugin-usage.v1"
      },
      "eventId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "occurredAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "sessionId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "packageId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 200
      },
      "packageVersion": {
        "type": "string",
        "minLength": 1,
        "maxLength": 80
      },
      "resourceKind": {
        "enum": [
          "extension",
          "tool",
          "command",
          "skill",
          "prompt",
          "theme"
        ]
      },
      "resourceId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 200
      },
      "activity": {
        "enum": [
          "loaded",
          "invoked",
          "finished"
        ]
      },
      "invocationId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "outcome": {
        "enum": [
          "succeeded",
          "failed",
          "cancelled"
        ]
      },
      "durationMs": {
        "type": "integer",
        "minimum": 0
      }
    },
    "allOf": [
      {
        "if": {
          "properties": {
            "activity": {
              "const": "loaded"
            }
          },
          "required": [
            "activity"
          ]
        },
        "then": {
          "not": {
            "anyOf": [
              {
                "required": [
                  "invocationId"
                ]
              },
              {
                "required": [
                  "outcome"
                ]
              },
              {
                "required": [
                  "durationMs"
                ]
              }
            ]
          }
        }
      },
      {
        "if": {
          "properties": {
            "activity": {
              "const": "invoked"
            }
          },
          "required": [
            "activity"
          ]
        },
        "then": {
          "required": [
            "invocationId"
          ],
          "not": {
            "anyOf": [
              {
                "required": [
                  "outcome"
                ]
              },
              {
                "required": [
                  "durationMs"
                ]
              }
            ]
          }
        }
      },
      {
        "if": {
          "properties": {
            "activity": {
              "const": "finished"
            }
          },
          "required": [
            "activity"
          ]
        },
        "then": {
          "required": [
            "invocationId",
            "outcome",
            "durationMs"
          ]
        }
      },
      {
        "if": {
          "properties": {
            "resourceKind": {
              "enum": [
                "extension",
                "theme"
              ]
            }
          },
          "required": [
            "resourceKind"
          ]
        },
        "then": {
          "properties": {
            "activity": {
              "const": "loaded"
            }
          }
        }
      },
      {
        "if": {
          "properties": {
            "resourceKind": {
              "const": "prompt"
            }
          },
          "required": [
            "resourceKind"
          ]
        },
        "then": {
          "properties": {
            "activity": {
              "enum": [
                "loaded",
                "invoked"
              ]
            }
          }
        }
      }
    ]
  },
  "pi-runtime-manifest.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.pi-runtime-manifest.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "runtimeVersion",
      "piVersion",
      "platform",
      "architecture",
      "launchKind",
      "piEntrypoint",
      "extensionEntrypoint",
      "tools",
      "createdAtMs",
      "source",
      "files"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.pi-runtime-manifest.v1"
      },
      "runtimeVersion": {
        "type": "string",
        "minLength": 1
      },
      "piVersion": {
        "type": "string",
        "minLength": 1
      },
      "runtimeProtocolVersion": {
        "type": "string",
        "enum": [
          "1",
          "2"
        ]
      },
      "runtimeMethods": {
        "type": "array",
        "items": {
          "type": "string",
          "minLength": 1
        },
        "uniqueItems": true
      },
      "platform": {
        "type": "string",
        "minLength": 1
      },
      "architecture": {
        "type": "string",
        "minLength": 1
      },
      "launchKind": {
        "type": "string",
        "enum": [
          "node",
          "standalone"
        ]
      },
      "piEntrypoint": {
        "type": "string",
        "minLength": 1
      },
      "nodeEntrypoint": {
        "type": "string"
      },
      "extensionEntrypoint": {
        "type": "string",
        "minLength": 1
      },
      "tools": {
        "type": "array",
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "source": {
        "type": "object",
        "required": [
          "repository",
          "commit",
          "package"
        ],
        "properties": {
          "repository": {
            "type": "string",
            "minLength": 1
          },
          "commit": {
            "type": "string",
            "minLength": 1
          },
          "package": {
            "type": "string",
            "minLength": 1
          },
          "sourceContractSha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$"
          },
          "handlersCommit": {
            "type": "string",
            "pattern": "^[0-9a-f]{40}$"
          }
        }
      },
      "files": {
        "type": "array",
        "items": {
          "type": "object",
          "required": [
            "path",
            "sha256",
            "byteSize",
            "executable"
          ],
          "properties": {
            "path": {
              "type": "string",
              "minLength": 1
            },
            "sha256": {
              "type": "string",
              "minLength": 64
            },
            "byteSize": {
              "type": "integer",
              "minimum": 0
            },
            "executable": {
              "type": "boolean"
            }
          }
        }
      }
    },
    "allOf": [
      {
        "if": {
          "properties": {
            "runtimeProtocolVersion": {
              "const": "2"
            }
          },
          "required": [
            "runtimeProtocolVersion"
          ]
        },
        "then": {
          "required": [
            "runtimeMethods"
          ],
          "properties": {
            "source": {
              "required": [
                "sourceContractSha256",
                "handlersCommit"
              ]
            }
          }
        }
      }
    ]
  },
  "prompt-compile-receipt.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "wisdom-weasel.prompt-compile-receipt.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "receiptId",
      "plan",
      "omittedLayers",
      "producerAudit",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "wisdom-weasel.prompt-compile-receipt.v1"
      },
      "receiptId": {
        "type": "string",
        "minLength": 1
      },
      "plan": {
        "type": "object"
      },
      "omittedLayers": {
        "type": "array",
        "items": {
          "type": "string"
        }
      },
      "producerAudit": {
        "type": "array",
        "items": {
          "type": "object"
        }
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      }
    }
  },
  "prompt-plan.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "wisdom-weasel.prompt-plan.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "bindingId",
      "roomId",
      "rootId",
      "sessionId",
      "journalId",
      "generation",
      "sessionEpoch",
      "contextEpoch",
      "capabilityRevision",
      "capabilityEpoch",
      "skillPolicyRevision",
      "contextPolicyRevision",
      "layers",
      "stablePrefixHash",
      "projectionHash",
      "throughSequence",
      "sealedProjectionRefs",
      "dynamicTailRefs",
      "planHash"
    ],
    "properties": {
      "schemaVersion": {
        "const": "wisdom-weasel.prompt-plan.v1"
      },
      "bindingId": {
        "type": "string",
        "minLength": 1
      },
      "roomId": {
        "type": "string",
        "minLength": 1
      },
      "rootId": {
        "type": "string",
        "minLength": 1
      },
      "sessionId": {
        "type": "string",
        "minLength": 1
      },
      "journalId": {
        "type": "string",
        "minLength": 1
      },
      "generation": {
        "type": "integer",
        "minimum": 0
      },
      "sessionEpoch": {
        "type": "integer",
        "minimum": 1
      },
      "contextEpoch": {
        "type": "integer",
        "minimum": 1
      },
      "capabilityRevision": {
        "type": "string",
        "minLength": 1
      },
      "capabilityEpoch": {
        "type": "integer",
        "minimum": 1
      },
      "skillPolicyRevision": {
        "type": "string",
        "minLength": 1
      },
      "contextPolicyRevision": {
        "type": "string",
        "minLength": 1
      },
      "layers": {
        "type": "array",
        "minItems": 6,
        "maxItems": 6,
        "items": {
          "type": "object"
        }
      },
      "stablePrefixHash": {
        "type": "string",
        "minLength": 64,
        "maxLength": 64
      },
      "projectionHash": {
        "type": "string",
        "minLength": 64,
        "maxLength": 64
      },
      "throughSequence": {
        "type": "integer",
        "minimum": 0
      },
      "sealedProjectionRefs": {
        "type": "array",
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "dynamicTailRefs": {
        "type": "array",
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "planHash": {
        "type": "string",
        "minLength": 64,
        "maxLength": 64
      }
    }
  },
  "provider-projection-journal.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://wisdom-weasel.local/contracts/provider-projection-journal.v1.json",
    "title": "Provider-only append and seal journal",
    "type": "object",
    "required": [
      "schemaVersion",
      "journalId",
      "rootId",
      "roomId",
      "bindingId",
      "sessionId",
      "sessionEpoch",
      "contextEpoch",
      "generation",
      "sealedThroughSequence",
      "revision"
    ],
    "properties": {
      "schemaVersion": {
        "const": "wisdom-weasel.provider-projection-journal.v1"
      },
      "journalId": {
        "type": "string",
        "minLength": 1
      },
      "rootId": {
        "type": "string",
        "minLength": 1
      },
      "roomId": {
        "type": "string",
        "minLength": 1
      },
      "bindingId": {
        "type": "string",
        "minLength": 1
      },
      "sessionId": {
        "type": "string",
        "minLength": 1
      },
      "sessionEpoch": {
        "type": "integer",
        "minimum": 1
      },
      "contextEpoch": {
        "type": "integer",
        "minimum": 1
      },
      "generation": {
        "type": "integer",
        "minimum": 0
      },
      "sealedThroughSequence": {
        "type": "integer",
        "minimum": 0
      },
      "revision": {
        "type": "integer",
        "minimum": 0
      }
    },
    "additionalProperties": false
  },
  "provider-projection-receipt.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://wisdom-weasel.local/contracts/provider-projection-receipt.v1.json",
    "title": "Provider projection seal receipt",
    "type": "object",
    "required": [
      "schemaVersion",
      "receiptId",
      "journalId",
      "providerRequestId",
      "generation",
      "sealedThroughSequence",
      "projectionHash",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "wisdom-weasel.provider-projection-receipt.v1"
      },
      "receiptId": {
        "type": "string",
        "minLength": 1
      },
      "journalId": {
        "type": "string",
        "minLength": 1
      },
      "providerRequestId": {
        "type": "string",
        "minLength": 1
      },
      "generation": {
        "type": "integer",
        "minimum": 0
      },
      "sealedThroughSequence": {
        "type": "integer",
        "minimum": 1
      },
      "projectionHash": {
        "type": "string",
        "minLength": 64
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      }
    },
    "additionalProperties": false
  },
  "reflection-dead-letter-projection.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "wisdom-weasel.reflection-dead-letter-projection.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "deadLetterId",
      "incidentId",
      "ownerRef",
      "reasonCode",
      "lastEvidenceRefs",
      "nextAction",
      "attemptCount",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "wisdom-weasel.reflection-dead-letter-projection.v1"
      },
      "deadLetterId": {
        "type": "string"
      },
      "incidentId": {
        "type": "string"
      },
      "ownerRef": {
        "type": "string"
      },
      "reasonCode": {
        "type": "string"
      },
      "lastEvidenceRefs": {
        "type": "array"
      },
      "nextAction": {
        "type": "string"
      },
      "attemptCount": {
        "type": "integer"
      },
      "createdAtMs": {
        "type": "integer"
      }
    }
  },
  "requirement-anchor.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "wisdom-weasel.requirement-anchor.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "anchorId",
      "rootId",
      "rootSequence",
      "originalContentSha256",
      "originalByteLength",
      "createdBy",
      "authenticity",
      "provenance",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "wisdom-weasel.requirement-anchor.v1"
      },
      "anchorId": {
        "type": "string",
        "minLength": 1
      },
      "rootId": {
        "type": "string",
        "minLength": 1
      },
      "rootSequence": {
        "type": "integer",
        "minimum": 1
      },
      "originalContentSha256": {
        "type": "string",
        "minLength": 64,
        "maxLength": 64
      },
      "originalByteLength": {
        "type": "integer",
        "minimum": 1
      },
      "createdBy": {
        "type": "string",
        "minLength": 1
      },
      "authenticity": {
        "enum": [
          "original_user_bytes",
          "legacy_quarantined"
        ]
      },
      "provenance": {
        "type": "object"
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      }
    }
  },
  "requirement-catalog-revision.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "wisdom-weasel.requirement-catalog-revision.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "catalogRevisionId",
      "rootId",
      "revision",
      "supersedesRevisionId",
      "anchorRefs",
      "items",
      "acceptanceCriteria",
      "changeReason",
      "provenance",
      "payloadHash",
      "createdBy",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "wisdom-weasel.requirement-catalog-revision.v1"
      },
      "catalogRevisionId": {
        "type": "string",
        "minLength": 1
      },
      "rootId": {
        "type": "string",
        "minLength": 1
      },
      "revision": {
        "type": "integer",
        "minimum": 1
      },
      "supersedesRevisionId": {
        "type": [
          "string",
          "null"
        ]
      },
      "anchorRefs": {
        "type": "array",
        "minItems": 1,
        "items": {
          "type": "string"
        }
      },
      "items": {
        "type": "array",
        "items": {
          "type": "object"
        }
      },
      "acceptanceCriteria": {
        "type": "array",
        "items": {
          "type": "object"
        }
      },
      "changeReason": {
        "type": "string",
        "minLength": 1
      },
      "provenance": {
        "type": "object"
      },
      "payloadHash": {
        "type": "string",
        "minLength": 64,
        "maxLength": 64
      },
      "createdBy": {
        "type": "string",
        "minLength": 1
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      }
    }
  },
  "rime-rank-selection.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.rime-rank-selection.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "selectionId",
      "sourceType",
      "preedit",
      "acceptedText",
      "candidateRank"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.rime-rank-selection.v1"
      },
      "selectionId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "sourceType": {
        "const": "rime"
      },
      "selectionSource": {
        "type": "string",
        "maxLength": 80
      },
      "preedit": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "acceptedText": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "rejectedText": {
        "type": "string",
        "maxLength": 240
      },
      "candidateRank": {
        "type": "integer",
        "minimum": 1,
        "maximum": 999
      },
      "shownCandidateCount": {
        "type": "integer",
        "minimum": 0,
        "maximum": 999
      },
      "app": {
        "type": "string",
        "maxLength": 240
      },
      "frontAppBundleId": {
        "type": "string",
        "maxLength": 240
      },
      "frontmostApp": {
        "type": "string",
        "maxLength": 240
      },
      "bundleId": {
        "type": "string",
        "maxLength": 240
      },
      "project": {
        "type": "string",
        "maxLength": 240
      },
      "privacyDisposition": {
        "type": "string",
        "enum": [
          "allowed",
          "sensitive",
          "unknown"
        ]
      },
      "sensitiveField": {
        "type": "boolean"
      },
      "secureInput": {
        "type": "boolean"
      },
      "dryRun": {
        "type": "boolean"
      }
    },
    "additionalProperties": false
  },
  "rime-select.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.rime-select.v1",
    "type": "object",
    "required": [
      "candidate"
    ],
    "properties": {
      "candidate": {
        "$ref": "#/$defs/candidate"
      },
      "shownCandidates": {
        "type": "array",
        "items": {
          "$ref": "#/$defs/candidate"
        }
      },
      "query": {
        "type": "string"
      },
      "recentContext": {
        "type": "string"
      },
      "committedContext": {
        "type": "string"
      },
      "preedit": {
        "type": "string"
      },
      "project": {
        "type": "string"
      },
      "app": {
        "type": "string"
      },
      "frontAppBundleId": {
        "type": "string"
      },
      "frontmostApp": {
        "type": "string"
      },
      "bundleId": {
        "type": "string"
      },
      "contextGroupId": {
        "type": "string"
      },
      "contextGroupLevel": {
        "type": "string"
      },
      "privacyDisposition": {
        "type": "string",
        "enum": [
          "allowed",
          "sensitive",
          "unknown"
        ]
      },
      "sensitiveField": {
        "type": "boolean"
      },
      "secureInput": {
        "type": "boolean"
      },
      "dryRun": {
        "type": "boolean"
      }
    },
    "$defs": {
      "candidate": {
        "type": "object",
        "required": [
          "insertText",
          "sourceType"
        ],
        "properties": {
          "text": {
            "type": "string"
          },
          "insertText": {
            "type": "string",
            "minLength": 1
          },
          "sourceType": {
            "type": "string"
          },
          "memoryId": {
            "type": "string"
          },
          "suggestionId": {
            "type": "string"
          },
          "sourceEventId": {
            "type": [
              "integer",
              "null"
            ]
          }
        }
      }
    }
  },
  "rime-suggest-request.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.rime-suggest-request.v1",
    "type": "object",
    "required": [
      "sessionId",
      "requestSeq"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string"
      },
      "sessionId": {
        "type": "string",
        "minLength": 1
      },
      "requestSeq": {
        "type": "integer",
        "minimum": 0
      },
      "rawInput": {
        "type": "string"
      },
      "preedit": {
        "type": "string"
      },
      "commitTextPreview": {
        "type": "string"
      },
      "committedContext": {
        "type": "string"
      },
      "project": {
        "type": "string"
      },
      "app": {
        "type": "string"
      },
      "privacyDisposition": {
        "type": "string",
        "enum": [
          "allowed",
          "sensitive",
          "unknown"
        ]
      },
      "privacyLeaseId": {
        "type": "string"
      },
      "privacyLeaseEpoch": {
        "type": "integer",
        "minimum": 0
      },
      "privacyFocusEpoch": {
        "type": "integer",
        "minimum": 0
      },
      "sensitiveField": {
        "type": "boolean"
      },
      "secureInput": {
        "type": "boolean"
      },
      "progressiveFollowUp": {
        "type": "boolean"
      },
      "frontendRevision": {
        "type": "integer",
        "minimum": 0
      },
      "selectionEpoch": {
        "type": "integer",
        "minimum": 0
      },
      "inputGeneration": {
        "type": "integer",
        "minimum": 0
      },
      "foregroundText": {
        "$ref": "#/$defs/foregroundContext"
      }
    },
    "$defs": {
      "foregroundContext": {
        "type": "object",
        "required": [
          "available",
          "source",
          "freshnessMs"
        ],
        "properties": {
          "available": {
            "type": "boolean"
          },
          "source": {
            "type": "string"
          },
          "freshnessMs": {
            "type": "integer",
            "minimum": 0
          },
          "surroundingBefore": {
            "type": "string"
          },
          "surroundingAfter": {
            "type": "string"
          },
          "selectedText": {
            "type": "string"
          },
          "canReplaceSelection": {
            "type": "boolean"
          },
          "contextGroupId": {
            "type": "string"
          },
          "contextGroupLevel": {
            "type": "string",
            "enum": [
              "document",
              "project",
              "app",
              "global",
              ""
            ]
          }
        }
      }
    }
  },
  "rime-suggest-response.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.rime-suggest-response.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "sessionId",
      "requestSeq",
      "displayCandidates",
      "predictionSession",
      "keyPolicy",
      "assistantOverlay"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "minLength": 1
      },
      "sessionId": {
        "type": "string",
        "minLength": 1
      },
      "requestSeq": {
        "type": "integer",
        "minimum": 0
      },
      "runtimeRevision": {
        "type": "integer",
        "minimum": 0
      },
      "runtimeConfig": {
        "type": "object"
      },
      "displayCandidates": {
        "type": "array"
      },
      "predictionSession": {
        "type": "object"
      },
      "keyPolicy": {
        "type": "object"
      },
      "assistantOverlay": {
        "type": "object"
      },
      "overlayConfig": {
        "type": "object"
      },
      "frontendTransaction": {
        "type": "object"
      },
      "stored": {
        "type": "boolean"
      },
      "noStore": {
        "type": "boolean"
      },
      "privacyAssessment": {
        "type": "object"
      },
      "storageReceipt": {
        "type": "object"
      }
    }
  },
  "role-book-curation.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.role-book-curation.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "traitProposals",
      "capabilityProposals",
      "lessonProposals",
      "commitmentProposals",
      "warnings"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.role-book-curation.v1"
      },
      "traitProposals": {
        "type": "array",
        "maxItems": 6,
        "items": {
          "$ref": "#/$defs/proposal"
        }
      },
      "capabilityProposals": {
        "type": "array",
        "maxItems": 12,
        "items": {
          "$ref": "#/$defs/proposal"
        }
      },
      "lessonProposals": {
        "type": "array",
        "maxItems": 8,
        "items": {
          "$ref": "#/$defs/proposal"
        }
      },
      "commitmentProposals": {
        "type": "array",
        "maxItems": 8,
        "items": {
          "$ref": "#/$defs/proposal"
        }
      },
      "warnings": {
        "type": "array",
        "maxItems": 16,
        "items": {
          "type": "string",
          "maxLength": 240
        }
      }
    },
    "$defs": {
      "proposal": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "text",
          "confidence",
          "sourceEvidenceIds",
          "reviewRequired"
        ],
        "properties": {
          "text": {
            "type": "string",
            "minLength": 1,
            "maxLength": 280
          },
          "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          },
          "sourceEvidenceIds": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "uniqueItems": true,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 240
            }
          },
          "reviewRequired": {
            "type": "boolean",
            "const": true
          }
        }
      }
    }
  },
  "role-book-revision-draft.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.role-book-revision-draft.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "draftId",
      "status",
      "project",
      "roleId",
      "baseRoleVersion",
      "sourceDigestId",
      "sourceEvidenceIds",
      "patch",
      "policy",
      "proposalDiagnostics",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.role-book-revision-draft.v1"
      },
      "draftId": {
        "type": "string",
        "minLength": 1
      },
      "status": {
        "type": "string",
        "const": "draft"
      },
      "project": {
        "type": "string"
      },
      "roleId": {
        "type": "string",
        "minLength": 1
      },
      "baseRoleVersion": {
        "type": "string",
        "minLength": 1
      },
      "sourceDigestId": {
        "type": "string",
        "minLength": 1
      },
      "sourceEvidenceIds": {
        "type": "array",
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "patch": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "recentWork",
          "traitProposals",
          "capabilityProposals",
          "lessonProposals",
          "commitmentProposals"
        ],
        "properties": {
          "recentWork": {
            "type": "array",
            "items": {
              "type": "object"
            }
          },
          "traitProposals": {
            "type": "array",
            "maxItems": 6,
            "items": {
              "$ref": "#/$defs/proposal"
            }
          },
          "capabilityProposals": {
            "type": "array",
            "maxItems": 12,
            "items": {
              "$ref": "#/$defs/proposal"
            }
          },
          "lessonProposals": {
            "type": "array",
            "maxItems": 8,
            "items": {
              "$ref": "#/$defs/proposal"
            }
          },
          "commitmentProposals": {
            "type": "array",
            "maxItems": 8,
            "items": {
              "$ref": "#/$defs/proposal"
            }
          }
        }
      },
      "policy": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "defaultApply",
          "safeAutoApplyFields",
          "reviewRequiredFields"
        ],
        "properties": {
          "defaultApply": {
            "type": "boolean",
            "const": false
          },
          "safeAutoApplyFields": {
            "type": "array",
            "items": {
              "type": "string",
              "enum": [
                "recentWork"
              ]
            }
          },
          "reviewRequiredFields": {
            "type": "array",
            "items": {
              "type": "string",
              "enum": [
                "traits",
                "capabilities",
                "lessonsAndLimits",
                "activeCommitments"
              ]
            }
          }
        }
      },
      "proposalDiagnostics": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "status",
          "provider",
          "inputChars",
          "acceptedProposalCount",
          "rejectedProposalCount"
        ],
        "properties": {
          "status": {
            "type": "string",
            "enum": [
              "not_configured",
              "no_conversation_evidence",
              "no_eligible_evidence",
              "unsupported",
              "completed",
              "failed"
            ]
          },
          "provider": {
            "type": "string",
            "maxLength": 80
          },
          "inputChars": {
            "type": "integer",
            "minimum": 0,
            "maximum": 12000
          },
          "acceptedProposalCount": {
            "type": "integer",
            "minimum": 0
          },
          "rejectedProposalCount": {
            "type": "integer",
            "minimum": 0
          },
          "error": {
            "type": "string",
            "maxLength": 400
          }
        }
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      }
    },
    "$defs": {
      "proposal": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "text",
          "confidence",
          "sourceEvidenceIds",
          "reviewRequired"
        ],
        "properties": {
          "text": {
            "type": "string",
            "minLength": 1,
            "maxLength": 280
          },
          "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          },
          "sourceEvidenceIds": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "uniqueItems": true,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 240
            }
          },
          "reviewRequired": {
            "type": "boolean",
            "const": true
          }
        }
      }
    }
  },
  "room-binding.v2": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://wisdom-weasel.local/contracts/room-binding.v2.json",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "bindingId",
      "rootId",
      "roomId",
      "participantId",
      "taskId",
      "generation",
      "protocolRevision",
      "capabilityRevision",
      "access"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "wisdom-weasel.room-binding.v2"
      },
      "bindingId": {
        "type": "string",
        "minLength": 1
      },
      "rootId": {
        "type": "string",
        "minLength": 1
      },
      "roomId": {
        "type": "string",
        "minLength": 1
      },
      "participantId": {
        "type": "string",
        "minLength": 1
      },
      "taskId": {
        "type": [
          "string",
          "null"
        ]
      },
      "generation": {
        "type": "integer",
        "minimum": 0
      },
      "protocolRevision": {
        "type": "string",
        "minLength": 1
      },
      "capabilityRevision": {
        "type": "string",
        "minLength": 1
      },
      "access": {
        "type": "string",
        "enum": [
          "read",
          "write"
        ]
      }
    }
  },
  "room-commit.v2": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://wisdom-weasel.local/contracts/room-commit.v2.json",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "commitId",
      "dispatchId",
      "action",
      "contentHash",
      "postProposal",
      "evidenceRefs",
      "requirementCoverage",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "wisdom-weasel.room-commit.v2"
      },
      "commitId": {
        "type": "string",
        "minLength": 1
      },
      "dispatchId": {
        "type": "string",
        "minLength": 1
      },
      "action": {
        "type": "string",
        "enum": [
          "post",
          "dispatch",
          "wait",
          "complete",
          "block"
        ]
      },
      "contentHash": {
        "type": "string",
        "minLength": 1
      },
      "postProposal": {
        "type": [
          "object",
          "null"
        ]
      },
      "postInvocationReceiptId": {
        "type": "string",
        "minLength": 1
      },
      "continuation": {
        "oneOf": [
          {
            "type": "null"
          },
          {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "decision"
            ],
            "properties": {
              "decision": {
                "type": "string",
                "enum": [
                  "dispatch",
                  "wait",
                  "block",
                  "complete"
                ]
              },
              "childTask": {
                "type": "object"
              },
              "childDispatch": {
                "type": "object"
              }
            },
            "allOf": [
              {
                "if": {
                  "properties": {
                    "decision": {
                      "const": "dispatch"
                    }
                  },
                  "required": [
                    "decision"
                  ]
                },
                "then": {
                  "required": [
                    "childTask",
                    "childDispatch"
                  ]
                }
              }
            ]
          }
        ]
      },
      "evidenceRefs": {
        "type": "array",
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "requirementCoverage": {
        "type": "array",
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      }
    }
  },
  "room-commit.v3": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://wisdom-weasel.local/contracts/room-commit.v3.json",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "commitId",
      "dispatchId",
      "action",
      "contentHash",
      "postProposal",
      "qualityGateReceipt",
      "evidenceRefs",
      "requirementCoverage",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "wisdom-weasel.room-commit.v3"
      },
      "commitId": {
        "type": "string",
        "minLength": 1
      },
      "dispatchId": {
        "type": "string",
        "minLength": 1
      },
      "action": {
        "type": "string",
        "enum": [
          "post",
          "dispatch",
          "wait",
          "complete",
          "block"
        ]
      },
      "contentHash": {
        "type": "string",
        "minLength": 1
      },
      "postProposal": {
        "type": [
          "object",
          "null"
        ]
      },
      "postInvocationReceiptId": {
        "type": "string",
        "minLength": 1
      },
      "continuation": {
        "oneOf": [
          {
            "type": "null"
          },
          {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "decision"
            ],
            "properties": {
              "decision": {
                "type": "string",
                "enum": [
                  "dispatch",
                  "wait",
                  "block",
                  "complete"
                ]
              },
              "childTask": {
                "type": "object"
              },
              "childDispatch": {
                "type": "object"
              },
              "waitForDispatchIds": {
                "type": "array",
                "maxItems": 32,
                "uniqueItems": true,
                "items": {
                  "type": "string",
                  "minLength": 1
                }
              },
              "waitingFor": {
                "type": "string",
                "enum": [
                  "user",
                  "participant",
                  "external"
                ]
              },
              "waitingForParticipantId": {
                "type": "string",
                "minLength": 1
              },
              "waitingForDispatchId": {
                "type": "string",
                "minLength": 1
              },
              "resumeCondition": {
                "type": "string",
                "minLength": 1
              },
              "question": {
                "type": "string",
                "minLength": 1
              },
              "questionOptions": {
                "type": "array",
                "minItems": 2,
                "maxItems": 5,
                "items": {
                  "$ref": "#/$defs/questionOption"
                }
              }
            },
            "allOf": [
              {
                "if": {
                  "properties": {
                    "decision": {
                      "const": "dispatch"
                    }
                  },
                  "required": [
                    "decision"
                  ]
                },
                "then": {
                  "required": [
                    "childTask",
                    "childDispatch"
                  ]
                }
              },
              {
                "if": {
                  "properties": {
                    "decision": {
                      "const": "wait"
                    }
                  },
                  "required": [
                    "decision"
                  ]
                },
                "then": {
                  "required": [
                    "waitingFor",
                    "resumeCondition"
                  ]
                }
              },
              {
                "if": {
                  "properties": {
                    "decision": {
                      "const": "wait"
                    },
                    "waitingFor": {
                      "const": "participant"
                    }
                  },
                  "required": [
                    "decision",
                    "waitingFor"
                  ]
                },
                "then": {
                  "required": [
                    "waitingForParticipantId",
                    "waitingForDispatchId"
                  ]
                }
              },
              {
                "if": {
                  "required": [
                    "questionOptions"
                  ]
                },
                "then": {
                  "properties": {
                    "decision": {
                      "const": "wait"
                    },
                    "waitingFor": {
                      "const": "user"
                    }
                  },
                  "required": [
                    "question",
                    "waitingFor"
                  ]
                }
              }
            ]
          }
        ]
      },
      "qualityGateReceipt": {
        "$ref": "#/$defs/qualityGateReceipt"
      },
      "evidenceRefs": {
        "type": "array",
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "requirementCoverage": {
        "type": "array",
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      }
    },
    "$defs": {
      "qualityGateReceipt": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "schemaVersion",
          "receiptId",
          "rootId",
          "taskId",
          "dispatchId",
          "generation",
          "originalRequestChecked",
          "verdict",
          "items",
          "residualRisks",
          "createdAtMs"
        ],
        "properties": {
          "schemaVersion": {
            "type": "string",
            "const": "wisdom-weasel.room-quality-gate-receipt.v1"
          },
          "receiptId": {
            "type": "string",
            "minLength": 1
          },
          "rootId": {
            "type": "string",
            "minLength": 1
          },
          "taskId": {
            "type": "string",
            "minLength": 1
          },
          "dispatchId": {
            "type": "string",
            "minLength": 1
          },
          "generation": {
            "type": "integer",
            "minimum": 0
          },
          "originalRequestChecked": {
            "type": "boolean"
          },
          "verdict": {
            "type": "string",
            "enum": [
              "ready_to_deliver",
              "not_ready"
            ]
          },
          "items": {
            "type": "array",
            "maxItems": 64,
            "items": {
              "type": "object",
              "additionalProperties": false,
              "required": [
                "criterionId",
                "status",
                "evidenceRefs"
              ],
              "properties": {
                "criterionId": {
                  "type": "string",
                  "minLength": 1
                },
                "status": {
                  "type": "string",
                  "enum": [
                    "pass",
                    "fail",
                    "not_verified"
                  ]
                },
                "evidenceRefs": {
                  "type": "array",
                  "maxItems": 64,
                  "items": {
                    "type": "string",
                    "minLength": 1
                  }
                }
              }
            }
          },
          "residualRisks": {
            "type": "array",
            "maxItems": 32,
            "items": {
              "type": "string",
              "minLength": 1
            }
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "questionOption": {
        "type": "object",
        "required": [
          "value",
          "label"
        ],
        "properties": {
          "value": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80
          },
          "label": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120
          },
          "description": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500
          },
          "recommended": {
            "type": "boolean"
          }
        },
        "additionalProperties": false
      }
    }
  },
  "room-dispatch-envelope.v2": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://wisdom-weasel.local/contracts/room-dispatch-envelope.v2.json",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "dispatchId",
      "rootId",
      "taskId",
      "parentDispatchId",
      "generation",
      "hopCount",
      "depth",
      "budgetCost",
      "targetSessionId",
      "targetParticipantId",
      "triggerId",
      "intentKind",
      "idempotencyKey",
      "attempt",
      "capabilityEpoch",
      "runtimeProfileRevision",
      "state"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "wisdom-weasel.room-dispatch-envelope.v2"
      },
      "dispatchId": {
        "type": "string",
        "minLength": 1
      },
      "rootId": {
        "type": "string",
        "minLength": 1
      },
      "taskId": {
        "type": "string",
        "minLength": 1
      },
      "parentDispatchId": {
        "type": [
          "string",
          "null"
        ]
      },
      "generation": {
        "type": "integer",
        "minimum": 0
      },
      "hopCount": {
        "type": "integer",
        "minimum": 0
      },
      "depth": {
        "type": "integer",
        "minimum": 0
      },
      "budgetCost": {
        "type": "integer",
        "minimum": 0
      },
      "targetSessionId": {
        "type": "string",
        "minLength": 1
      },
      "targetParticipantId": {
        "type": "string",
        "minLength": 1
      },
      "triggerId": {
        "type": "string",
        "minLength": 1
      },
      "intentKind": {
        "type": "string",
        "enum": [
          "align",
          "execute",
          "review",
          "revise",
          "resume",
          "retry",
          "wake",
          "callback",
          "close"
        ]
      },
      "idempotencyKey": {
        "type": "string",
        "minLength": 1
      },
      "attempt": {
        "type": "integer",
        "minimum": 0
      },
      "capabilityEpoch": {
        "type": "integer",
        "minimum": 0
      },
      "runtimeProfileRevision": {
        "type": "string",
        "minLength": 1
      },
      "alignmentOrdinal": {
        "type": "integer",
        "minimum": 0
      },
      "dependsOnDispatchIds": {
        "type": "array",
        "maxItems": 16,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "attachmentIds": {
        "type": "array",
        "maxItems": 8,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "pattern": "^media_[A-Za-z0-9_-]{12,80}$"
        }
      },
      "state": {
        "type": "string",
        "enum": [
          "pending",
          "leased",
          "running",
          "retry_wait",
          "timer_wait",
          "committed",
          "unknown",
          "dead_letter",
          "failed",
          "cancelled"
        ]
      }
    }
  },
  "room-event-envelope.v2": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://wisdom-weasel.local/contracts/room-event-envelope.v2.json",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "entityKind",
      "entityId",
      "eventKind",
      "sequence",
      "occurredAtMs",
      "payload"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "wisdom-weasel.room-event-envelope.v2"
      },
      "entityKind": {
        "type": "string",
        "enum": [
          "root",
          "task",
          "dispatch",
          "commit",
          "post",
          "binding"
        ]
      },
      "entityId": {
        "type": "string",
        "minLength": 1
      },
      "eventKind": {
        "type": "string",
        "minLength": 1
      },
      "sequence": {
        "type": "integer",
        "minimum": 1
      },
      "occurredAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "payload": {
        "type": "object"
      }
    }
  },
  "room-permission-policy.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.room-permission-policy.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "room",
      "partner",
      "toolAgent"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.room-permission-policy.v1"
      },
      "room": {
        "$ref": "#/$defs/roomPermissionLayer"
      },
      "partner": {
        "$ref": "#/$defs/roomPermissionLowerLayer"
      },
      "toolAgent": {
        "$ref": "#/$defs/roomPermissionLowerLayer"
      }
    },
    "$defs": {
      "roomPermissionLayer": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "executionMode"
        ],
        "properties": {
          "executionMode": {
            "type": "string",
            "enum": [
              "read_only",
              "per_action",
              "workspace_managed",
              "full_trust"
            ]
          }
        }
      },
      "roomPermissionLowerLayer": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "executionMode"
        ],
        "properties": {
          "executionMode": {
            "type": "string",
            "enum": [
              "inherit",
              "read_only",
              "per_action",
              "workspace_managed",
              "full_trust"
            ]
          }
        }
      }
    }
  },
  "room-post.v2": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://wisdom-weasel.local/contracts/room-post.v2.json",
    "title": "Explicit Room Post",
    "type": "object",
    "required": [
      "schemaVersion",
      "postId",
      "roomId",
      "rootId",
      "generation",
      "authorActorRef",
      "kind",
      "visibility",
      "content",
      "idempotencyKey",
      "publicationSource",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "wisdom-weasel.room-post.v2"
      },
      "postId": {
        "type": "string",
        "minLength": 1
      },
      "roomId": {
        "type": "string",
        "minLength": 1
      },
      "rootId": {
        "type": "string",
        "minLength": 1
      },
      "generation": {
        "type": "integer",
        "minimum": 0
      },
      "taskId": {
        "type": "string"
      },
      "dispatchId": {
        "type": "string"
      },
      "authorActorRef": {
        "type": "string",
        "minLength": 1
      },
      "kind": {
        "type": "string",
        "minLength": 1
      },
      "visibility": {
        "enum": [
          "room",
          "root"
        ]
      },
      "content": {
        "type": "string",
        "minLength": 1
      },
      "question": {
        "type": "object",
        "required": [
          "prompt",
          "options"
        ],
        "properties": {
          "prompt": {
            "type": "string",
            "minLength": 1
          },
          "options": {
            "type": "array",
            "anyOf": [
              {
                "maxItems": 0
              },
              {
                "minItems": 2,
                "maxItems": 5
              }
            ],
            "items": {
              "$ref": "#/$defs/questionOption"
            }
          }
        },
        "additionalProperties": false
      },
      "mentions": {
        "type": "array",
        "maxItems": 16,
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "blocks": {
        "type": "array",
        "items": {
          "type": "object",
          "required": [
            "schemaVersion",
            "id",
            "type",
            "status",
            "presentationKind",
            "data",
            "summary",
            "source",
            "visibility",
            "digest",
            "ref",
            "generation"
          ],
          "properties": {
            "schemaVersion": {
              "const": "rag-ime.agent-block.v1"
            },
            "id": {
              "type": "string",
              "minLength": 1
            },
            "type": {
              "enum": [
                "text",
                "code",
                "reasoning_summary",
                "progress",
                "tool_call",
                "tool_result",
                "citation",
                "image",
                "audio",
                "file",
                "sticker",
                "task_plan",
                "diff",
                "approval",
                "error",
                "card",
                "checklist",
                "table",
                "artifact",
                "reference",
                "status",
                "unknown"
              ]
            },
            "status": {
              "enum": [
                "queued",
                "running",
                "completed",
                "failed",
                "aborted"
              ]
            },
            "presentationKind": {
              "type": "string",
              "minLength": 1
            },
            "data": {
              "type": "object"
            },
            "summary": {
              "type": "string",
              "maxLength": 240
            },
            "source": {
              "type": "object"
            },
            "visibility": {
              "enum": [
                "room_post",
                "root_post"
              ]
            },
            "digest": {
              "type": "string",
              "pattern": "^[0-9a-f]{64}$"
            },
            "ref": {
              "type": "string",
              "minLength": 1
            },
            "generation": {
              "type": "integer",
              "minimum": 0
            }
          }
        }
      },
      "attachments": {
        "type": "array",
        "maxItems": 8,
        "items": {
          "type": "object",
          "additionalProperties": false,
          "required": [
            "schemaVersion",
            "mediaId",
            "ownerType",
            "ownerId",
            "roomId",
            "fileName",
            "mimeType",
            "byteSize",
            "sha256",
            "origin",
            "createdAtMs"
          ],
          "properties": {
            "schemaVersion": {
              "const": "rag-ime.agent-media.v1"
            },
            "mediaId": {
              "type": "string",
              "pattern": "^media_[A-Za-z0-9_-]{12,80}$"
            },
            "ownerType": {
              "const": "room"
            },
            "ownerId": {
              "type": "string",
              "minLength": 1
            },
            "roomId": {
              "type": "string",
              "minLength": 1
            },
            "fileName": {
              "type": "string",
              "maxLength": 160
            },
            "mimeType": {
              "enum": [
                "image/png",
                "image/jpeg",
                "image/gif",
                "image/webp"
              ]
            },
            "byteSize": {
              "type": "integer",
              "minimum": 1,
              "maximum": 20971520
            },
            "sha256": {
              "type": "string",
              "pattern": "^[0-9a-f]{64}$"
            },
            "width": {
              "type": [
                "integer",
                "null"
              ]
            },
            "height": {
              "type": [
                "integer",
                "null"
              ]
            },
            "durationMs": {
              "type": [
                "integer",
                "null"
              ]
            },
            "thumbnailMediaId": {
              "type": [
                "string",
                "null"
              ]
            },
            "origin": {
              "const": "user_attachment"
            },
            "originTool": {
              "type": "string"
            },
            "originReceiptId": {
              "type": "string"
            },
            "createdAtMs": {
              "type": "integer",
              "minimum": 0
            }
          }
        }
      },
      "workResult": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "proposedOperabilityVerdict",
          "proposedRequirementVerdict"
        ],
        "properties": {
          "proposedOperabilityVerdict": {
            "type": "string",
            "enum": [
              "passed",
              "failed",
              "unverified"
            ]
          },
          "proposedRequirementVerdict": {
            "type": "string",
            "enum": [
              "satisfied",
              "not_satisfied",
              "unverified"
            ]
          }
        }
      },
      "idempotencyKey": {
        "type": "string",
        "minLength": 1
      },
      "publicationSource": {
        "type": "object",
        "required": [
          "kind",
          "ref"
        ],
        "properties": {
          "kind": {
            "enum": [
              "user",
              "room_commit",
              "room_post",
              "runtime_projection"
            ]
          },
          "ref": {
            "type": "string",
            "minLength": 1
          }
        },
        "additionalProperties": false
      },
      "chronology": {
        "$ref": "#/$defs/chronology"
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      }
    },
    "$defs": {
      "chronology": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "schemaVersion",
          "roomEventId",
          "roomEventSequence",
          "createdAtMs",
          "afterPostId",
          "orderKey"
        ],
        "properties": {
          "schemaVersion": {
            "const": "wisdom-weasel.room-post-chronology.v1"
          },
          "roomEventId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 320
          },
          "roomEventSequence": {
            "type": "integer",
            "minimum": 1
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "afterPostId": {
            "type": [
              "string",
              "null"
            ],
            "maxLength": 320
          },
          "orderKey": {
            "type": "string",
            "pattern": "^room-event:[0-9]{20}$"
          }
        }
      },
      "questionOption": {
        "type": "object",
        "required": [
          "value",
          "label"
        ],
        "properties": {
          "value": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80
          },
          "label": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120
          },
          "description": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500
          },
          "recommended": {
            "type": "boolean"
          }
        },
        "additionalProperties": false
      }
    },
    "additionalProperties": false
  },
  "room-quality-gate-receipt.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://wisdom-weasel.local/contracts/room-quality-gate-receipt.v1.json",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "receiptId",
      "rootId",
      "taskId",
      "dispatchId",
      "generation",
      "originalRequestChecked",
      "verdict",
      "items",
      "residualRisks",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "wisdom-weasel.room-quality-gate-receipt.v1"
      },
      "receiptId": {
        "type": "string",
        "minLength": 1
      },
      "rootId": {
        "type": "string",
        "minLength": 1
      },
      "taskId": {
        "type": "string",
        "minLength": 1
      },
      "dispatchId": {
        "type": "string",
        "minLength": 1
      },
      "generation": {
        "type": "integer",
        "minimum": 0
      },
      "originalRequestChecked": {
        "type": "boolean"
      },
      "verdict": {
        "type": "string",
        "enum": [
          "ready_to_deliver",
          "not_ready"
        ]
      },
      "items": {
        "type": "array",
        "maxItems": 64,
        "items": {
          "type": "object",
          "additionalProperties": false,
          "required": [
            "criterionId",
            "status",
            "evidenceRefs"
          ],
          "properties": {
            "criterionId": {
              "type": "string",
              "minLength": 1
            },
            "status": {
              "type": "string",
              "enum": [
                "pass",
                "fail",
                "not_verified"
              ]
            },
            "evidenceRefs": {
              "type": "array",
              "maxItems": 64,
              "items": {
                "type": "string",
                "minLength": 1
              }
            }
          }
        }
      },
      "residualRisks": {
        "type": "array",
        "maxItems": 32,
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      }
    }
  },
  "room-root-execution.v3": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://wisdom-weasel.local/contracts/room-root-execution.v3.json",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "rootId",
      "roomId",
      "generation",
      "state",
      "facilitatorParticipantId",
      "reporterParticipantId",
      "reporterSelectionReceiptId",
      "requirementAnchorRef",
      "createdByActorRef",
      "terminalReceiptId",
      "activeProfileRef",
      "budgetPolicyRef",
      "independentReviewRequired"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "wisdom-weasel.room-root-execution.v3"
      },
      "rootId": {
        "type": "string",
        "minLength": 1
      },
      "roomId": {
        "type": "string",
        "minLength": 1
      },
      "generation": {
        "type": "integer",
        "minimum": 0
      },
      "state": {
        "type": "string",
        "enum": [
          "pending",
          "running",
          "waiting",
          "blocked",
          "cancelling",
          "cancelled",
          "cancelled_with_unknowns",
          "completed",
          "failed"
        ]
      },
      "facilitatorParticipantId": {
        "type": "string",
        "minLength": 1
      },
      "reporterParticipantId": {
        "type": [
          "string",
          "null"
        ]
      },
      "reporterSelectionReceiptId": {
        "type": [
          "string",
          "null"
        ]
      },
      "requirementAnchorRef": {
        "type": "string",
        "minLength": 1
      },
      "createdByActorRef": {
        "type": "string",
        "minLength": 1
      },
      "terminalReceiptId": {
        "type": [
          "string",
          "null"
        ]
      },
      "activeProfileRef": {
        "type": [
          "string",
          "null"
        ]
      },
      "budgetPolicyRef": {
        "type": "string",
        "minLength": 1
      },
      "independentReviewRequired": {
        "type": "boolean"
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      }
    }
  },
  "room-shadow-observation.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://wisdom-weasel.local/contracts/room-shadow-observation.v1.json",
    "title": "Room V2 shadow observation envelope",
    "type": "object",
    "required": [
      "schemaVersion",
      "rootId",
      "triggerId",
      "recordKind",
      "entityId",
      "payload"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.room-shadow-observation.v1"
      },
      "rootId": {
        "type": "string",
        "minLength": 1
      },
      "taskId": {
        "type": "string"
      },
      "dispatchId": {
        "type": "string"
      },
      "triggerId": {
        "type": "string",
        "minLength": 1
      },
      "recordKind": {
        "type": "string",
        "minLength": 1
      },
      "entityId": {
        "type": "string",
        "minLength": 1
      },
      "payload": {
        "type": "object"
      }
    },
    "additionalProperties": false
  },
  "room-task.v3": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://wisdom-weasel.local/contracts/room-task.v3.json",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "taskId",
      "rootId",
      "parentTaskId",
      "taskKind",
      "currentOwnerParticipantId",
      "ownershipRevision",
      "ownershipReceiptId",
      "objective",
      "expectedOutput",
      "requirementItemIds",
      "acceptanceCriterionIds",
      "contextEvidenceRefs",
      "invitationId",
      "reviewOfTaskIds",
      "reviewAuthorParticipantIds",
      "reviewState",
      "revision",
      "state"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "wisdom-weasel.room-task.v3"
      },
      "taskId": {
        "type": "string",
        "minLength": 1
      },
      "rootId": {
        "type": "string",
        "minLength": 1
      },
      "parentTaskId": {
        "type": [
          "string",
          "null"
        ]
      },
      "taskKind": {
        "type": "string",
        "enum": [
          "work",
          "invitation",
          "review",
          "report"
        ]
      },
      "workItemId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 320
      },
      "currentOwnerParticipantId": {
        "type": "string",
        "minLength": 1
      },
      "ownershipRevision": {
        "type": "integer",
        "minimum": 0
      },
      "ownershipReceiptId": {
        "type": [
          "string",
          "null"
        ]
      },
      "objective": {
        "type": "string",
        "minLength": 1
      },
      "expectedOutput": {
        "type": "string",
        "minLength": 1
      },
      "requirementItemIds": {
        "type": "array",
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "acceptanceCriterionIds": {
        "type": "array",
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "contextEvidenceRefs": {
        "type": "array",
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "invitationId": {
        "type": [
          "string",
          "null"
        ]
      },
      "reviewOfTaskIds": {
        "type": "array",
        "uniqueItems": true,
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "reviewAuthorParticipantIds": {
        "type": "array",
        "uniqueItems": true,
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "reviewState": {
        "type": "string",
        "enum": [
          "not_required",
          "required",
          "in_review",
          "accepted",
          "accepted_with_notes",
          "changes_requested",
          "disputed",
          "escalated",
          "stale"
        ]
      },
      "resultSummary": {
        "type": "string",
        "maxLength": 2000
      },
      "resultKind": {
        "type": "string",
        "enum": [
          "complete",
          "dispatch",
          "wait",
          "post",
          "block"
        ]
      },
      "resultAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "verificationCount": {
        "type": "integer",
        "minimum": 0,
        "maximum": 8192
      },
      "verifications": {
        "type": "array",
        "maxItems": 64,
        "items": {
          "$ref": "#/$defs/publicVerification"
        }
      },
      "artifactRefs": {
        "type": "array",
        "maxItems": 128,
        "items": {
          "type": "string",
          "minLength": 1,
          "maxLength": 1000
        }
      },
      "residualRisks": {
        "type": "array",
        "maxItems": 32,
        "items": {
          "type": "string",
          "minLength": 1,
          "maxLength": 1000
        }
      },
      "reviewTargetRevision": {
        "type": "string",
        "pattern": "^sha256:[0-9a-f]{64}$"
      },
      "reviewEvidenceNotBeforeMs": {
        "type": "integer",
        "minimum": 0
      },
      "reviewRound": {
        "type": "integer",
        "minimum": 1
      },
      "reviewFindings": {
        "type": "array",
        "maxItems": 64,
        "items": {
          "$ref": "#/$defs/reviewFinding"
        }
      },
      "workspacePolicy": {
        "enum": [
          "read_only",
          "shared_single_writer",
          "isolated_writable"
        ]
      },
      "workspaceRoot": {
        "type": "string",
        "minLength": 1
      },
      "workspaceBaseRoot": {
        "type": "string",
        "minLength": 1
      },
      "workspaceBaseCommit": {
        "type": "string",
        "minLength": 1
      },
      "workspaceSnapshotSha256": {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$"
      },
      "workspaceBindingId": {
        "type": "string",
        "minLength": 1
      },
      "workspaceRepositoryId": {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$"
      },
      "workspaceLifecycleState": {
        "type": "string",
        "enum": [
          "reserved",
          "materialized",
          "work_started",
          "delivered",
          "integration_started",
          "integrated",
          "conflict",
          "failed",
          "blocked",
          "cancelled",
          "orphaned",
          "incomplete",
          "retained",
          "retry_bound",
          "abandoned",
          "cleanup_failed",
          "cleaned"
        ]
      },
      "workspaceCleanupState": {
        "type": "string",
        "enum": [
          "not_authorized",
          "authorized",
          "retained",
          "cleaned",
          "missing",
          "failed"
        ]
      },
      "workspaceAttentionRequired": {
        "type": "boolean"
      },
      "workspaceDeliveryRevision": {
        "type": "string",
        "pattern": "^sha256:[0-9a-f]{64}$"
      },
      "workspaceDeliveryHead": {
        "type": "string",
        "minLength": 1
      },
      "workspaceDeliverySnapshotSha256": {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$"
      },
      "workspaceDelivery": {
        "$ref": "#/$defs/workspaceDelivery"
      },
      "workspaceIntegrationPatchSha256": {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$"
      },
      "workspaceIntegratedRevision": {
        "type": "string",
        "minLength": 1
      },
      "workspaceIntegratedSnapshotSha256": {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$"
      },
      "workspaceTerminalReason": {
        "type": "string",
        "maxLength": 2000
      },
      "workspaceIntegrationState": {
        "enum": [
          "not_required",
          "pending",
          "applied"
        ]
      },
      "workspaceIntegrationRef": {
        "type": [
          "string",
          "null"
        ]
      },
      "workspaceRestorePolicy": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "mode",
          "toolProfileVersion",
          "executionMode",
          "workspaceScopeGranted",
          "workspaceScopeSha256",
          "workspaceScopeGrantedAtMs",
          "toolAllowlistMode",
          "allowedTools",
          "projectContextEnabled",
          "piSkillsEnabled",
          "codexSkillsEnabled"
        ],
        "properties": {
          "mode": {
            "type": "string",
            "enum": [
              "assistant",
              "coordinator"
            ]
          },
          "toolProfileVersion": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120
          },
          "executionMode": {
            "type": "string",
            "enum": [
              "read_only",
              "per_action",
              "workspace_managed",
              "full_trust"
            ]
          },
          "workspaceScopeGranted": {
            "type": "boolean"
          },
          "workspaceScopeSha256": {
            "type": "string",
            "pattern": "^(|[0-9a-f]{64})$"
          },
          "workspaceScopeGrantedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "toolAllowlistMode": {
            "type": "string",
            "enum": [
              "profile",
              "explicit"
            ]
          },
          "allowedTools": {
            "type": "array",
            "maxItems": 128,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 160
            }
          },
          "projectContextEnabled": {
            "type": "boolean"
          },
          "piSkillsEnabled": {
            "type": "boolean"
          },
          "codexSkillsEnabled": {
            "type": "boolean"
          }
        }
      },
      "revision": {
        "type": "integer",
        "minimum": 0
      },
      "state": {
        "type": "string",
        "enum": [
          "pending",
          "active",
          "review",
          "waiting",
          "blocked",
          "completed",
          "failed",
          "cancelled"
        ]
      }
    },
    "$defs": {
      "publicVerification": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "label",
          "result",
          "source"
        ],
        "properties": {
          "label": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120,
            "pattern": "^(?!.*(?:AC-|ac:|criterionId|criterion:)).+$"
          },
          "result": {
            "type": "string",
            "enum": [
              "pass",
              "fail",
              "not_verified",
              "recorded"
            ]
          },
          "source": {
            "type": "string",
            "const": "quality_gate"
          }
        }
      },
      "workspaceDelivery": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "schemaVersion",
          "ownerParticipantId",
          "ownerSessionId",
          "workItemId",
          "taskId",
          "deliveryRevision",
          "baseCommit",
          "workspaceSnapshotSha256",
          "patchSha256",
          "deliveredAtMs",
          "resultSummary",
          "manifestSha256",
          "files",
          "totals",
          "artifactRefs",
          "verificationCount",
          "verifications",
          "verificationRefs",
          "residualRisks"
        ],
        "properties": {
          "schemaVersion": {
            "type": "string",
            "const": "wisdom-weasel.room-workspace-delivery.v1"
          },
          "ownerParticipantId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 320
          },
          "ownerSessionId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 320
          },
          "workItemId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 320
          },
          "taskId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 320
          },
          "deliveryRevision": {
            "type": "string",
            "pattern": "^sha256:[0-9a-f]{64}$"
          },
          "baseCommit": {
            "type": "string",
            "minLength": 1
          },
          "workspaceSnapshotSha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$"
          },
          "patchSha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$"
          },
          "deliveredAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "resultSummary": {
            "type": "string",
            "maxLength": 2000
          },
          "manifestSha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$"
          },
          "files": {
            "type": "array",
            "maxItems": 512,
            "items": {
              "$ref": "#/$defs/workspaceDeliveryFile"
            }
          },
          "totals": {
            "$ref": "#/$defs/workspaceDeliveryTotals"
          },
          "artifactRefs": {
            "type": "array",
            "maxItems": 128,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 1000
            }
          },
          "verificationCount": {
            "type": "integer",
            "minimum": 0,
            "maximum": 8192
          },
          "verifications": {
            "type": "array",
            "maxItems": 64,
            "items": {
              "$ref": "#/$defs/publicVerification"
            }
          },
          "verificationRefs": {
            "type": "array",
            "maxItems": 128,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 1000
            }
          },
          "residualRisks": {
            "type": "array",
            "maxItems": 32,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 1000
            }
          }
        }
      },
      "workspaceDeliveryFile": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "path",
          "additions",
          "deletions",
          "binary",
          "generated",
          "redacted"
        ],
        "properties": {
          "path": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4096
          },
          "additions": {
            "type": [
              "integer",
              "null"
            ],
            "minimum": 0
          },
          "deletions": {
            "type": [
              "integer",
              "null"
            ],
            "minimum": 0
          },
          "binary": {
            "type": "boolean"
          },
          "generated": {
            "type": "boolean"
          },
          "redacted": {
            "type": "boolean"
          }
        }
      },
      "workspaceDeliveryTotals": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "fileCount",
          "additions",
          "deletions",
          "binaryFiles",
          "generatedFiles",
          "redactedFiles"
        ],
        "properties": {
          "fileCount": {
            "type": "integer",
            "minimum": 0,
            "maximum": 512
          },
          "additions": {
            "type": "integer",
            "minimum": 0
          },
          "deletions": {
            "type": "integer",
            "minimum": 0
          },
          "binaryFiles": {
            "type": "integer",
            "minimum": 0,
            "maximum": 512
          },
          "generatedFiles": {
            "type": "integer",
            "minimum": 0,
            "maximum": 512
          },
          "redactedFiles": {
            "type": "integer",
            "minimum": 0,
            "maximum": 512
          }
        }
      },
      "reviewFinding": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "findingId",
          "fingerprint",
          "gateEffect",
          "impact",
          "category",
          "scope",
          "observation",
          "expected",
          "userImpact",
          "evidenceRefs",
          "reproduction",
          "state",
          "firstSeenRevision",
          "lastCheckedRevision",
          "failedRechecks",
          "response"
        ],
        "properties": {
          "findingId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "fingerprint": {
            "type": "string",
            "pattern": "^sha256:[0-9a-f]{64}$"
          },
          "gateEffect": {
            "type": "string",
            "enum": [
              "blocking",
              "advisory"
            ]
          },
          "impact": {
            "type": "string",
            "enum": [
              "critical",
              "high",
              "normal"
            ]
          },
          "category": {
            "type": "string",
            "enum": [
              "correctness",
              "security",
              "privacy",
              "data_loss",
              "authorization",
              "permission",
              "destructive_behavior",
              "core_runtime_unavailable",
              "regression",
              "review_target_identity",
              "spec_mismatch",
              "ux",
              "performance",
              "maintainability",
              "test",
              "documentation"
            ]
          },
          "scope": {
            "type": "object",
            "additionalProperties": false,
            "properties": {
              "criterionId": {
                "type": "string",
                "minLength": 1
              },
              "invariantId": {
                "type": "string",
                "minLength": 1
              }
            },
            "anyOf": [
              {
                "required": [
                  "criterionId"
                ]
              },
              {
                "required": [
                  "invariantId"
                ]
              }
            ]
          },
          "observation": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2000
          },
          "expected": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2000
          },
          "userImpact": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2000
          },
          "evidenceRefs": {
            "type": "array",
            "minItems": 1,
            "maxItems": 64,
            "uniqueItems": true,
            "items": {
              "type": "string",
              "minLength": 1
            }
          },
          "reproduction": {
            "type": "array",
            "minItems": 1,
            "maxItems": 16,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 1000
            }
          },
          "state": {
            "type": "string",
            "enum": [
              "open",
              "resolved",
              "dismissed",
              "accepted_risk",
              "contested",
              "escalated"
            ]
          },
          "dispositionRationale": {
            "type": [
              "string",
              "null"
            ],
            "minLength": 1,
            "maxLength": 2000
          },
          "ownerParticipantId": {
            "type": [
              "string",
              "null"
            ],
            "minLength": 1
          },
          "firstSeenRevision": {
            "type": "string",
            "pattern": "^sha256:[0-9a-f]{64}$"
          },
          "lastCheckedRevision": {
            "type": "string",
            "pattern": "^sha256:[0-9a-f]{64}$"
          },
          "failedRechecks": {
            "type": "integer",
            "minimum": 0,
            "maximum": 2
          },
          "response": {
            "oneOf": [
              {
                "type": "null"
              },
              {
                "type": "object",
                "additionalProperties": false,
                "required": [
                  "findingId",
                  "action",
                  "rationale",
                  "evidenceRefs",
                  "participantId",
                  "createdAtMs"
                ],
                "properties": {
                  "findingId": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 160
                  },
                  "action": {
                    "type": "string",
                    "enum": [
                      "fixed",
                      "contest"
                    ]
                  },
                  "rationale": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000
                  },
                  "evidenceRefs": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 64,
                    "uniqueItems": true,
                    "items": {
                      "type": "string",
                      "minLength": 1
                    }
                  },
                  "participantId": {
                    "type": "string",
                    "minLength": 1
                  },
                  "createdAtMs": {
                    "type": "integer",
                    "minimum": 0
                  }
                }
              }
            ]
          }
        }
      }
    }
  },
  "runner-verification-receipt.v2": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "wisdom-weasel.runner-verification-receipt.v2",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "receiptId",
      "rootId",
      "catalogRevisionId",
      "receiptType",
      "sourceCommit",
      "environment",
      "worktreeHash",
      "commandOrAction",
      "exitStatus",
      "outputHash",
      "artifactHash",
      "toolVersion",
      "issuerId",
      "contentHash",
      "issuerSignature",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "wisdom-weasel.runner-verification-receipt.v2"
      },
      "receiptId": {
        "type": "string",
        "minLength": 1
      },
      "rootId": {
        "type": "string",
        "minLength": 1
      },
      "catalogRevisionId": {
        "type": "string",
        "minLength": 1
      },
      "receiptType": {
        "enum": [
          "test",
          "build",
          "install",
          "browser",
          "evidence"
        ]
      },
      "sourceCommit": {
        "type": "string",
        "minLength": 1
      },
      "environment": {
        "type": "string",
        "minLength": 1
      },
      "worktreeHash": {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$"
      },
      "commandOrAction": {
        "type": "string",
        "minLength": 1
      },
      "exitStatus": {
        "type": "integer"
      },
      "outputHash": {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$"
      },
      "artifactHash": {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$"
      },
      "toolVersion": {
        "type": "string",
        "minLength": 1
      },
      "issuerId": {
        "type": "string",
        "minLength": 1
      },
      "contentHash": {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$"
      },
      "issuerSignature": {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$"
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      }
    }
  },
  "sandbox-run.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.sandbox-run.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "sandboxRunId",
      "appId",
      "status",
      "policy",
      "traceIds",
      "evalRunIds",
      "createdAtMs",
      "updatedAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.sandbox-run.v1"
      },
      "sandboxRunId": {
        "type": "string",
        "minLength": 1
      },
      "appId": {
        "type": "string",
        "minLength": 1
      },
      "status": {
        "enum": [
          "queued",
          "running",
          "completed",
          "failed",
          "cancelled"
        ]
      },
      "policy": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "workspaceBindingId",
          "workspaceFingerprint",
          "mutationMode",
          "network",
          "productionWriteBlocked"
        ],
        "properties": {
          "workspaceBindingId": {
            "type": "string",
            "minLength": 1
          },
          "workspaceFingerprint": {
            "type": "string",
            "pattern": "^sha256:[a-f0-9]{64}$"
          },
          "mutationMode": {
            "enum": [
              "read_only",
              "staged"
            ]
          },
          "network": {
            "enum": [
              "blocked",
              "allowlisted"
            ]
          },
          "productionWriteBlocked": {
            "const": true
          }
        }
      },
      "replayCohort": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "suiteId",
          "suiteRevision",
          "caseId",
          "inputFingerprint",
          "environmentFingerprint",
          "configFingerprint",
          "modelProfileFingerprint",
          "toolProfileFingerprint",
          "skillProfileFingerprint"
        ],
        "properties": {
          "suiteId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "suiteRevision": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "caseId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "inputFingerprint": {
            "type": "string",
            "pattern": "^sha256:[a-f0-9]{64}$"
          },
          "environmentFingerprint": {
            "type": "string",
            "pattern": "^sha256:[a-f0-9]{64}$"
          },
          "configFingerprint": {
            "type": "string",
            "pattern": "^sha256:[a-f0-9]{64}$"
          },
          "modelProfileFingerprint": {
            "type": "string",
            "pattern": "^sha256:[a-f0-9]{64}$"
          },
          "toolProfileFingerprint": {
            "type": "string",
            "pattern": "^sha256:[a-f0-9]{64}$"
          },
          "skillProfileFingerprint": {
            "type": "string",
            "pattern": "^sha256:[a-f0-9]{64}$"
          }
        }
      },
      "traceIds": {
        "type": "array",
        "uniqueItems": true,
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "evalRunIds": {
        "type": "array",
        "uniqueItems": true,
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "updatedAtMs": {
        "type": "integer",
        "minimum": 0
      }
    }
  },
  "session-memory-recall.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.session-memory-recall.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "recallId",
      "sessionId",
      "project",
      "roleId",
      "generatedAtMs",
      "trigger",
      "query",
      "retrieval",
      "items",
      "sourceIds",
      "budget",
      "policy"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.session-memory-recall.v1"
      },
      "recallId": {
        "type": "string",
        "minLength": 1
      },
      "sessionId": {
        "type": "string",
        "minLength": 1
      },
      "project": {
        "type": "string"
      },
      "roleId": {
        "type": "string"
      },
      "generatedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "trigger": {
        "enum": [
          "first_user_prompt",
          "turn_start",
          "compaction",
          "room_task",
          "subagent_task"
        ]
      },
      "query": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "preview",
          "sha256",
          "recentCompleteInputCount",
          "recentCompleteInputUsedForRetrieval",
          "retrievalContextUsed",
          "recentConversationCount",
          "timelineIntent"
        ],
        "properties": {
          "preview": {
            "type": "string"
          },
          "sha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$"
          },
          "recentCompleteInputCount": {
            "type": "integer",
            "minimum": 0
          },
          "recentCompleteInputUsedForRetrieval": {
            "type": "boolean"
          },
          "retrievalContextUsed": {
            "type": "boolean"
          },
          "recentConversationCount": {
            "type": "integer",
            "minimum": 0,
            "maximum": 8
          },
          "timelineIntent": {
            "$ref": "#/$defs/timelineIntent"
          }
        }
      },
      "retrieval": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "strategy",
          "primaryQuery",
          "matchedAliases",
          "activatedTags",
          "visibleOwners",
          "requestedEmbeddingProvider",
          "embeddingProvider",
          "embeddingFallback",
          "temporalIntent",
          "timelineIntent",
          "activityTimelineIncluded",
          "vectorFusion"
        ],
        "properties": {
          "strategy": {
            "const": "vcp_hybrid_book_atom"
          },
          "primaryQuery": {
            "type": "string"
          },
          "matchedAliases": {
            "type": "array",
            "items": {
              "type": "string"
            }
          },
          "activatedTags": {
            "type": "array",
            "items": {
              "type": "string"
            }
          },
          "visibleOwners": {
            "type": "array",
            "items": {
              "type": "object",
              "additionalProperties": false,
              "required": [
                "ownerKind",
                "ownerId"
              ],
              "properties": {
                "ownerKind": {
                  "type": "string"
                },
                "ownerId": {
                  "type": "string",
                  "minLength": 1
                }
              }
            }
          },
          "requestedEmbeddingProvider": {
            "type": "string"
          },
          "embeddingProvider": {
            "type": "string"
          },
          "embeddingFallback": {
            "type": "boolean"
          },
          "temporalIntent": {
            "type": "boolean"
          },
          "timelineIntent": {
            "$ref": "#/$defs/timelineIntent"
          },
          "activityTimelineIncluded": {
            "type": "boolean"
          },
          "vectorFusion": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "applied",
              "queryWeight",
              "contextWeight"
            ],
            "properties": {
              "applied": {
                "type": "boolean"
              },
              "queryWeight": {
                "type": "number",
                "minimum": 0,
                "maximum": 1
              },
              "contextWeight": {
                "type": "number",
                "minimum": 0,
                "maximum": 0.5
              }
            }
          }
        }
      },
      "items": {
        "type": "array",
        "maxItems": 12,
        "items": {
          "type": "object",
          "additionalProperties": false,
          "required": [
            "rank",
            "sourceType",
            "sourceId",
            "title",
            "text",
            "score",
            "confidence",
            "lanes",
            "rawScores",
            "tags",
            "ownerKind",
            "ownerId",
            "evidenceEventIds"
          ],
          "properties": {
            "rank": {
              "type": "integer",
              "minimum": 1
            },
            "sourceType": {
              "enum": [
                "memory_book",
                "memory_atom",
                "memory_timeline"
              ]
            },
            "sourceId": {
              "type": "string",
              "minLength": 1
            },
            "title": {
              "type": "string"
            },
            "text": {
              "type": "string",
              "minLength": 1
            },
            "score": {
              "type": "number"
            },
            "confidence": {
              "type": "number",
              "minimum": 0,
              "maximum": 1
            },
            "lanes": {
              "type": "array",
              "items": {
                "type": "string"
              }
            },
            "rawScores": {
              "type": "object",
              "additionalProperties": {
                "type": "number"
              }
            },
            "tags": {
              "type": "array",
              "items": {
                "type": "string"
              }
            },
            "ownerKind": {
              "type": "string"
            },
            "ownerId": {
              "type": "string"
            },
            "evidenceEventIds": {
              "type": "array",
              "items": {
                "type": "integer",
                "minimum": 1
              }
            },
            "project": {
              "type": "string"
            },
            "claimState": {
              "type": "string"
            },
            "validFromMs": {
              "type": "integer",
              "minimum": 0
            },
            "validToMs": {
              "type": "integer",
              "minimum": 0
            },
            "supersedesId": {
              "type": "string"
            },
            "sourceUpdatedAtMs": {
              "type": "integer",
              "minimum": 0
            },
            "sourceStartMs": {
              "type": "integer",
              "minimum": 0
            },
            "sourceEndMs": {
              "type": "integer",
              "minimum": 0
            },
            "corroborationOnly": {
              "type": "boolean"
            },
            "maySupportFacts": {
              "type": "boolean"
            }
          }
        }
      },
      "recentConversation": {
        "type": "array",
        "maxItems": 8,
        "items": {
          "type": "object",
          "additionalProperties": false,
          "required": [
            "role",
            "text"
          ],
          "properties": {
            "role": {
              "enum": [
                "user",
                "assistant"
              ]
            },
            "text": {
              "type": "string",
              "minLength": 1,
              "maxLength": 1200
            }
          }
        }
      },
      "todo": {
        "type": "array",
        "maxItems": 8,
        "items": {
          "type": "object",
          "additionalProperties": false,
          "required": [
            "status",
            "content"
          ],
          "properties": {
            "status": {
              "enum": [
                "pending",
                "in_progress",
                "blocked"
              ]
            },
            "content": {
              "type": "string",
              "minLength": 1,
              "maxLength": 240
            }
          }
        }
      },
      "task": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "kind": {
            "type": "string",
            "maxLength": 40
          },
          "objective": {
            "type": "string",
            "maxLength": 1200
          },
          "expectedOutput": {
            "type": "string",
            "maxLength": 800
          },
          "state": {
            "type": "string",
            "maxLength": 40
          },
          "acceptanceCriteria": {
            "type": "array",
            "maxItems": 8,
            "items": {
              "type": "string",
              "maxLength": 300
            }
          },
          "originalRequirements": {
            "type": "array",
            "maxItems": 4,
            "items": {
              "type": "string",
              "maxLength": 2000
            }
          }
        }
      },
      "compactionRecovery": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "schemaVersion",
          "summaryPresent",
          "summarySha256",
          "summaryChars",
          "skills",
          "tools"
        ],
        "properties": {
          "schemaVersion": {
            "const": "rag-ime.agent-compaction-recovery.v2"
          },
          "summaryPresent": {
            "type": "boolean"
          },
          "summarySha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$"
          },
          "summaryChars": {
            "type": "integer",
            "minimum": 0,
            "maximum": 8000
          },
          "skills": {
            "type": "array",
            "maxItems": 32,
            "items": {
              "type": "object",
              "additionalProperties": false,
              "required": [
                "name",
                "contentRevision"
              ],
              "properties": {
                "name": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 128
                },
                "contentRevision": {
                  "type": "string",
                  "pattern": "^[0-9a-f]{64}$"
                }
              }
            }
          },
          "tools": {
            "type": "array",
            "maxItems": 32,
            "items": {
              "type": "object",
              "additionalProperties": false,
              "required": [
                "name",
                "schemaRevision"
              ],
              "properties": {
                "name": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 128
                },
                "schemaRevision": {
                  "type": "string",
                  "pattern": "^[0-9a-f]{64}$"
                }
              }
            }
          }
        }
      },
      "coverage": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "status",
          "processedThroughAtMs",
          "lastRunAtMs"
        ],
        "properties": {
          "status": {
            "type": "string"
          },
          "processedThroughAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "lastRunAtMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "sourceIds": {
        "type": "array",
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "budget": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "maxItems",
          "maxChars",
          "usedChars",
          "omittedCount"
        ],
        "properties": {
          "maxItems": {
            "type": "integer",
            "minimum": 1
          },
          "maxChars": {
            "type": "integer",
            "minimum": 1
          },
          "usedChars": {
            "type": "integer",
            "minimum": 0
          },
          "omittedCount": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "policy": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "priority",
          "lifecycle",
          "evidenceOnly",
          "currentUserMessageWins",
          "rawRecentInputInjected",
          "recentConversationInjected",
          "detailLevel"
        ],
        "properties": {
          "priority": {
            "const": "developer"
          },
          "lifecycle": {
            "const": "session"
          },
          "evidenceOnly": {
            "const": true
          },
          "currentUserMessageWins": {
            "const": true
          },
          "rawRecentInputInjected": {
            "const": false
          },
          "recentConversationInjected": {
            "type": "boolean"
          },
          "detailLevel": {
            "enum": [
              "compact",
              "balanced",
              "detailed"
            ]
          }
        }
      }
    },
    "$defs": {
      "timelineIntent": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "requested",
          "reason",
          "matched",
          "range"
        ],
        "properties": {
          "requested": {
            "type": "boolean"
          },
          "reason": {
            "enum": [
              "none",
              "disabled",
              "exact_date",
              "explicit_timeline",
              "relative_time"
            ]
          },
          "matched": {
            "type": "array",
            "maxItems": 8,
            "items": {
              "type": "string"
            }
          },
          "range": {
            "type": "string"
          }
        }
      }
    }
  },
  "session-recall-effect-receipt.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.session-recall-effect-receipt.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "fixtureSha256",
      "candidateWeights",
      "selected"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.session-recall-effect-receipt.v1"
      },
      "fixtureSha256": {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$"
      },
      "candidateWeights": {
        "type": "array",
        "minItems": 5,
        "items": {
          "type": "object",
          "additionalProperties": false,
          "required": [
            "queryWeight",
            "summaryWeight",
            "score",
            "metrics",
            "selections"
          ],
          "properties": {
            "queryWeight": {
              "type": "number",
              "minimum": 0,
              "maximum": 1
            },
            "summaryWeight": {
              "type": "number",
              "minimum": 0,
              "maximum": 0.5
            },
            "score": {
              "type": "integer"
            },
            "metrics": {
              "type": "object",
              "additionalProperties": false,
              "required": [
                "factHit",
                "preferenceHit",
                "projectHit",
                "taskHit",
                "irrelevantInjection",
                "crossScopeLeak",
                "duplicateBytes",
                "tokenBytes",
                "compactionForgettingRecovery",
                "wrongOldTopic"
              ],
              "properties": {
                "factHit": {
                  "type": "integer",
                  "minimum": 0
                },
                "preferenceHit": {
                  "type": "integer",
                  "minimum": 0
                },
                "projectHit": {
                  "type": "integer",
                  "minimum": 0
                },
                "taskHit": {
                  "type": "integer",
                  "minimum": 0
                },
                "irrelevantInjection": {
                  "type": "integer",
                  "minimum": 0
                },
                "crossScopeLeak": {
                  "type": "integer",
                  "minimum": 0
                },
                "duplicateBytes": {
                  "type": "integer",
                  "minimum": 0
                },
                "tokenBytes": {
                  "type": "integer",
                  "minimum": 0
                },
                "compactionForgettingRecovery": {
                  "type": "integer",
                  "minimum": 0
                },
                "wrongOldTopic": {
                  "type": "integer",
                  "minimum": 0
                }
              }
            },
            "selections": {
              "type": "array",
              "items": {
                "type": "object",
                "additionalProperties": false,
                "required": [
                  "caseId",
                  "selected",
                  "fusion"
                ],
                "properties": {
                  "caseId": {
                    "type": "string",
                    "minLength": 1
                  },
                  "selected": {
                    "type": "array",
                    "items": {
                      "type": "string"
                    }
                  },
                  "fusion": {
                    "type": "object",
                    "additionalProperties": false,
                    "required": [
                      "applied",
                      "queryWeight",
                      "contextWeight"
                    ],
                    "properties": {
                      "applied": {
                        "type": "boolean"
                      },
                      "queryWeight": {
                        "type": "number"
                      },
                      "contextWeight": {
                        "type": "number"
                      }
                    }
                  }
                }
              }
            }
          }
        }
      },
      "selected": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "queryWeight",
          "summaryWeight",
          "reason"
        ],
        "properties": {
          "queryWeight": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          },
          "summaryWeight": {
            "type": "number",
            "minimum": 0,
            "maximum": 0.5
          },
          "reason": {
            "type": "string",
            "minLength": 1
          }
        }
      }
    }
  },
  "trace-diagnostic-inspection.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.trace-diagnostic-inspection.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "generatedAtMs",
      "targets",
      "traceIds",
      "timeline",
      "evidence",
      "scorecard",
      "truncated"
    ],
    "properties": {
      "intent": {
        "$ref": "#/$defs/optimizationIntent"
      },
      "schemaVersion": {
        "const": "rag-ime.trace-diagnostic-inspection.v1"
      },
      "generatedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "targets": {
        "type": "array",
        "minItems": 1,
        "maxItems": 12,
        "items": {
          "$ref": "#/$defs/target"
        }
      },
      "traceIds": {
        "type": "array",
        "maxItems": 32,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "minLength": 1,
          "maxLength": 240
        }
      },
      "timeline": {
        "type": "array",
        "maxItems": 240,
        "items": {
          "$ref": "#/$defs/timeline"
        }
      },
      "evidence": {
        "type": "array",
        "maxItems": 512,
        "items": {
          "$ref": "#/$defs/evidence"
        }
      },
      "requirements": {
        "$ref": "#/$defs/requirements"
      },
      "environment": {
        "$ref": "#/$defs/environment"
      },
      "scorecard": {
        "$ref": "#/$defs/scorecard"
      },
      "truncated": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "timeline",
          "evidence",
          "traceIds"
        ],
        "properties": {
          "timeline": {
            "type": "boolean"
          },
          "evidence": {
            "type": "boolean"
          },
          "traceIds": {
            "type": "boolean"
          }
        }
      }
    },
    "$defs": {
      "optimizationIntent": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "mode",
          "scopeMode",
          "focusAreas",
          "objective"
        ],
        "properties": {
          "mode": {
            "enum": [
              "improve",
              "distill"
            ]
          },
          "scopeMode": {
            "enum": [
              "all",
              "selected"
            ]
          },
          "focusAreas": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "items": {
              "enum": [
                "tool",
                "skill",
                "prompt",
                "workflow",
                "model"
              ],
              "type": "string"
            },
            "uniqueItems": true
          },
          "objective": {
            "type": "string",
            "minLength": 0,
            "maxLength": 2000
          }
        }
      },
      "target": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "targetKey",
          "kind",
          "id",
          "title",
          "traceIds",
          "sourceAvailable"
        ],
        "properties": {
          "targetKey": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500
          },
          "kind": {
            "enum": [
              "session",
              "room",
              "run"
            ]
          },
          "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "title": {
            "type": "string",
            "maxLength": 240
          },
          "traceIds": {
            "type": "array",
            "maxItems": 32,
            "uniqueItems": true,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 240
            }
          },
          "sourceAvailable": {
            "type": "boolean"
          }
        }
      },
      "timeline": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "evidenceId",
          "targetKey",
          "kind",
          "status",
          "summary",
          "sequence",
          "createdAtMs",
          "sourceRef",
          "traceId"
        ],
        "properties": {
          "evidenceId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "targetKey": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500
          },
          "kind": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80
          },
          "status": {
            "type": "string",
            "maxLength": 80
          },
          "summary": {
            "type": "string",
            "maxLength": 1200
          },
          "sequence": {
            "type": "number"
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "sourceRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "traceId": {
            "type": "string",
            "maxLength": 240
          }
        }
      },
      "evidence": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "evidenceId",
          "targetKey",
          "sourceKind",
          "sourceRef",
          "status",
          "summary",
          "createdAtMs",
          "traceId"
        ],
        "properties": {
          "evidenceId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "targetKey": {
            "type": "string",
            "maxLength": 500
          },
          "sourceKind": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80
          },
          "sourceRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "status": {
            "type": "string",
            "maxLength": 80
          },
          "summary": {
            "type": "string",
            "maxLength": 1200
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "traceId": {
            "type": "string",
            "maxLength": 240
          }
        }
      },
      "requirement": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "requirementId",
          "statement",
          "targetKey",
          "sourceRef",
          "evidenceIds"
        ],
        "properties": {
          "requirementId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "statement": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2000
          },
          "targetKey": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500
          },
          "sourceRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "evidenceIds": {
            "type": "array",
            "minItems": 1,
            "maxItems": 16,
            "uniqueItems": true,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            }
          }
        }
      },
      "requirements": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "source",
          "items",
          "truncated"
        ],
        "properties": {
          "source": {
            "enum": [
              "user_input",
              "work_item",
              "eval",
              "unknown"
            ]
          },
          "items": {
            "type": "array",
            "maxItems": 100,
            "items": {
              "$ref": "#/$defs/requirement"
            }
          },
          "truncated": {
            "type": "boolean"
          }
        }
      },
      "environmentTarget": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "targetKey",
          "sourceSha256",
          "modelProfile",
          "toolProfileVersion",
          "executionMode",
          "policyRevision",
          "workspaceScopeSha256",
          "shellPolicyVersion",
          "runtimeKind",
          "runtimeGeneration",
          "traceInputFingerprints",
          "traceStatuses"
        ],
        "properties": {
          "targetKey": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500
          },
          "sourceSha256": {
            "type": "string",
            "pattern": "^$|^[a-f0-9]{64}$"
          },
          "modelProfile": {
            "type": "string",
            "maxLength": 160
          },
          "toolProfileVersion": {
            "type": "string",
            "maxLength": 160
          },
          "executionMode": {
            "type": "string",
            "maxLength": 80
          },
          "policyRevision": {
            "type": [
              "integer",
              "null"
            ],
            "minimum": 0
          },
          "workspaceScopeSha256": {
            "type": "string",
            "pattern": "^$|^[a-f0-9]{64}$"
          },
          "shellPolicyVersion": {
            "type": "string",
            "maxLength": 160
          },
          "runtimeKind": {
            "type": "string",
            "maxLength": 80
          },
          "runtimeGeneration": {
            "type": [
              "integer",
              "null"
            ],
            "minimum": 0
          },
          "traceInputFingerprints": {
            "type": "array",
            "maxItems": 32,
            "uniqueItems": true,
            "items": {
              "type": "string",
              "pattern": "^sha256:[a-f0-9]{64}$"
            }
          },
          "traceStatuses": {
            "type": "array",
            "maxItems": 32,
            "items": {
              "type": "string",
              "maxLength": 80
            }
          }
        }
      },
      "environment": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "capturedAtMs",
          "rubricVersion",
          "targets",
          "limitations"
        ],
        "properties": {
          "capturedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "rubricVersion": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "targets": {
            "type": "array",
            "minItems": 1,
            "maxItems": 12,
            "items": {
              "$ref": "#/$defs/environmentTarget"
            }
          },
          "limitations": {
            "type": "array",
            "maxItems": 32,
            "uniqueItems": true,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 500
            }
          }
        }
      },
      "metric": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "metricId",
          "label",
          "value",
          "unit",
          "authority",
          "evidenceIds",
          "note"
        ],
        "properties": {
          "metricId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120
          },
          "label": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "value": {
            "type": [
              "number",
              "null"
            ]
          },
          "unit": {
            "type": "string",
            "maxLength": 40
          },
          "authority": {
            "enum": [
              "deterministic",
              "ground_truth",
              "ai_judge_estimate"
            ]
          },
          "evidenceIds": {
            "type": "array",
            "maxItems": 128,
            "uniqueItems": true,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            }
          },
          "note": {
            "type": "string",
            "maxLength": 500
          }
        }
      },
      "dimension": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "dimensionId",
          "title",
          "applicability",
          "authority",
          "score",
          "scoreMax",
          "metrics",
          "evidenceIds",
          "note"
        ],
        "properties": {
          "dimensionId": {
            "enum": [
              "task_completion",
              "evidence_diagnosis",
              "tool_runtime",
              "context",
              "room_collaboration",
              "memory_rag",
              "efficiency",
              "repair_quality"
            ]
          },
          "title": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "applicability": {
            "enum": [
              "measured",
              "partial",
              "not_applicable",
              "unavailable",
              "unknown"
            ]
          },
          "authority": {
            "enum": [
              "deterministic",
              "ground_truth",
              "mixed"
            ]
          },
          "score": {
            "type": [
              "number",
              "null"
            ],
            "minimum": 0,
            "maximum": 100
          },
          "scoreMax": {
            "const": 100
          },
          "metrics": {
            "type": "array",
            "maxItems": 32,
            "items": {
              "$ref": "#/$defs/metric"
            }
          },
          "evidenceIds": {
            "type": "array",
            "maxItems": 256,
            "uniqueItems": true,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            }
          },
          "note": {
            "type": "string",
            "maxLength": 800
          }
        }
      },
      "gate": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "gateId",
          "status",
          "evidenceIds",
          "reason"
        ],
        "properties": {
          "gateId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120
          },
          "status": {
            "enum": [
              "passed",
              "failed",
              "unknown"
            ]
          },
          "evidenceIds": {
            "type": "array",
            "maxItems": 128,
            "uniqueItems": true,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            }
          },
          "reason": {
            "type": "string",
            "maxLength": 500
          }
        }
      },
      "scorecard": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "rubricVersion",
          "hardGates",
          "dimensions",
          "comparison"
        ],
        "properties": {
          "rubricVersion": {
            "const": "trace-score-v1"
          },
          "hardGates": {
            "type": "array",
            "maxItems": 16,
            "items": {
              "$ref": "#/$defs/gate"
            }
          },
          "dimensions": {
            "type": "array",
            "minItems": 8,
            "maxItems": 8,
            "items": {
              "$ref": "#/$defs/dimension"
            }
          },
          "comparison": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "eligible",
              "status",
              "reason"
            ],
            "properties": {
              "eligible": {
                "type": "boolean"
              },
              "status": {
                "enum": [
                  "comparable",
                  "conditionally_comparable",
                  "incomparable",
                  "unknown"
                ]
              },
              "reason": {
                "type": "string",
                "maxLength": 800
              }
            }
          }
        }
      }
    }
  },
  "trace-diagnostic-report-list.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.trace-diagnostic-report-list.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "total",
      "truncated",
      "items"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.trace-diagnostic-report-list.v1"
      },
      "total": {
        "type": "integer",
        "minimum": 0
      },
      "truncated": {
        "type": "boolean"
      },
      "nextCursor": {
        "type": [
          "string",
          "null"
        ],
        "pattern": "^[0-9]+\\.[a-f0-9]{32}$"
      },
      "items": {
        "type": "array",
        "maxItems": 100,
        "items": {
          "$ref": "#/$defs/item"
        }
      }
    },
    "$defs": {
      "optimizationIntent": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "mode",
          "scopeMode",
          "focusAreas",
          "objective"
        ],
        "properties": {
          "mode": {
            "enum": [
              "improve",
              "distill"
            ]
          },
          "scopeMode": {
            "enum": [
              "all",
              "selected"
            ]
          },
          "focusAreas": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "items": {
              "enum": [
                "tool",
                "skill",
                "prompt",
                "workflow",
                "model"
              ],
              "type": "string"
            },
            "uniqueItems": true
          },
          "objective": {
            "type": "string",
            "minLength": 0,
            "maxLength": 2000
          }
        }
      },
      "item": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "reportId",
          "revision",
          "status",
          "title",
          "diagnosticSessionId",
          "targetKeys",
          "targets",
          "traceIds",
          "failureReason",
          "createdAtMs",
          "updatedAtMs"
        ],
        "properties": {
          "intent": {
            "$ref": "#/$defs/optimizationIntent"
          },
          "optimizationProjectId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256
          },
          "reportId": {
            "type": "string",
            "pattern": "^trace-report:[a-f0-9]{32}$"
          },
          "revision": {
            "type": "integer",
            "minimum": 1
          },
          "status": {
            "enum": [
              "generating",
              "completed",
              "failed"
            ]
          },
          "title": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "diagnosticSessionId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "targetKeys": {
            "type": "array",
            "minItems": 1,
            "maxItems": 12,
            "uniqueItems": true,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 500
            }
          },
          "targets": {
            "type": "array",
            "minItems": 1,
            "maxItems": 12,
            "items": {
              "$ref": "#/$defs/target"
            }
          },
          "traceIds": {
            "type": "array",
            "maxItems": 32,
            "uniqueItems": true,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 240
            }
          },
          "repairState": {
            "enum": [
              "not_recorded",
              "authorized",
              "verified",
              "failed"
            ]
          },
          "failureReason": {
            "type": "string",
            "maxLength": 1000
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "updatedAtMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "target": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "targetKey",
          "kind",
          "id",
          "title",
          "traceIds",
          "sourceAvailable"
        ],
        "properties": {
          "targetKey": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500
          },
          "kind": {
            "enum": [
              "session",
              "room",
              "run"
            ]
          },
          "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "title": {
            "type": "string",
            "maxLength": 240
          },
          "traceIds": {
            "type": "array",
            "maxItems": 32,
            "uniqueItems": true,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 240
            }
          },
          "sourceAvailable": {
            "type": "boolean"
          }
        }
      }
    }
  },
  "trace-diagnostic-report.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.trace-diagnostic-report.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "reportId",
      "revision",
      "status",
      "title",
      "diagnosticSessionId",
      "targets",
      "traceIds",
      "inspectionSha256",
      "inspection",
      "result",
      "failureReason",
      "createdAtMs",
      "updatedAtMs"
    ],
    "properties": {
      "intent": {
        "$ref": "#/$defs/optimizationIntent"
      },
      "optimizationProjectId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 256
      },
      "optimization": {
        "$ref": "#/$defs/optimization"
      },
      "distillation": {
        "$ref": "#/$defs/distillation"
      },
      "schemaVersion": {
        "const": "rag-ime.trace-diagnostic-report.v1"
      },
      "reportId": {
        "type": "string",
        "pattern": "^trace-report:[a-f0-9]{32}$"
      },
      "revision": {
        "type": "integer",
        "minimum": 1
      },
      "status": {
        "enum": [
          "generating",
          "completed",
          "failed"
        ]
      },
      "title": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "diagnosticSessionId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240
      },
      "targets": {
        "type": "array",
        "minItems": 1,
        "maxItems": 12,
        "items": {
          "$ref": "#/$defs/target"
        }
      },
      "traceIds": {
        "type": "array",
        "maxItems": 32,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "minLength": 1,
          "maxLength": 240
        }
      },
      "inspectionSha256": {
        "type": "string",
        "pattern": "^[a-f0-9]{64}$"
      },
      "inspection": {
        "type": "object"
      },
      "result": {
        "type": [
          "object",
          "null"
        ]
      },
      "repairLifecycle": {
        "$ref": "#/$defs/repairLifecycle"
      },
      "failureReason": {
        "type": "string",
        "maxLength": 1000
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "updatedAtMs": {
        "type": "integer",
        "minimum": 0
      }
    },
    "$defs": {
      "optimizationIntent": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "mode",
          "scopeMode",
          "focusAreas",
          "objective"
        ],
        "properties": {
          "mode": {
            "enum": [
              "improve",
              "distill"
            ]
          },
          "scopeMode": {
            "enum": [
              "all",
              "selected"
            ]
          },
          "focusAreas": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "items": {
              "enum": [
                "tool",
                "skill",
                "prompt",
                "workflow",
                "model"
              ],
              "type": "string"
            },
            "uniqueItems": true
          },
          "objective": {
            "type": "string",
            "minLength": 0,
            "maxLength": 2000
          }
        }
      },
      "optimizationChange": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "kind",
          "targetRef",
          "beforeVersionRef",
          "afterVersionRef"
        ],
        "properties": {
          "kind": {
            "enum": [
              "tool",
              "skill",
              "prompt",
              "workflow",
              "model"
            ]
          },
          "targetRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "beforeVersionRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "afterVersionRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          }
        }
      },
      "optimizationQualityGate": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "metricId",
          "minimum"
        ],
        "properties": {
          "metricId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "minimum": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          }
        }
      },
      "optimizationContract": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "caseSetRef",
          "caseIds",
          "controls",
          "declaredChanges",
          "qualityGates",
          "costMetric"
        ],
        "properties": {
          "caseSetRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "caseIds": {
            "type": "array",
            "minItems": 1,
            "maxItems": 128,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 240
            },
            "uniqueItems": true
          },
          "controls": {
            "type": "object",
            "additionalProperties": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            },
            "required": [
              "inputState",
              "evaluator",
              "qualityPolicy",
              "permissions",
              "environment"
            ]
          },
          "declaredChanges": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "items": {
              "$ref": "#/$defs/optimizationChange"
            }
          },
          "qualityGates": {
            "type": "array",
            "minItems": 1,
            "maxItems": 16,
            "items": {
              "$ref": "#/$defs/optimizationQualityGate"
            }
          },
          "costMetric": {
            "enum": [
              "",
              "totalCost"
            ]
          }
        }
      },
      "optimizationProposal": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "targetKind",
          "targetRef",
          "parentVersionRef",
          "candidateVersionRef",
          "findingIds",
          "evidenceIds",
          "historicalPatternRefs",
          "summary",
          "expectedEffect",
          "actualDiffRef",
          "comparisonContract"
        ],
        "properties": {
          "targetKind": {
            "enum": [
              "tool",
              "skill",
              "prompt",
              "workflow",
              "model"
            ]
          },
          "targetRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "parentVersionRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "candidateVersionRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "findingIds": {
            "type": "array",
            "minItems": 0,
            "maxItems": 128,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            },
            "uniqueItems": true
          },
          "evidenceIds": {
            "type": "array",
            "minItems": 1,
            "maxItems": 128,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            },
            "uniqueItems": true
          },
          "historicalPatternRefs": {
            "type": "array",
            "minItems": 0,
            "maxItems": 128,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            },
            "uniqueItems": true
          },
          "summary": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2000
          },
          "expectedEffect": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2000
          },
          "actualDiffRef": {
            "type": "string",
            "minLength": 0,
            "maxLength": 640
          },
          "comparisonContract": {
            "$ref": "#/$defs/optimizationContract"
          }
        }
      },
      "optimizationCandidate": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "targetKind",
          "targetRef",
          "parentVersionRef",
          "candidateVersionRef",
          "findingIds",
          "evidenceIds",
          "historicalPatternRefs",
          "summary",
          "expectedEffect",
          "actualDiffRef",
          "comparisonContract",
          "candidateId",
          "reportId",
          "optimizationProjectId",
          "comparisonContractSha256",
          "intent",
          "executionStatus",
          "diffStatus",
          "availableActions",
          "supportedActions",
          "createdAtMs",
          "contentSha256"
        ],
        "properties": {
          "targetKind": {
            "enum": [
              "tool",
              "skill",
              "prompt",
              "workflow",
              "model"
            ]
          },
          "targetRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "parentVersionRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "candidateVersionRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "findingIds": {
            "type": "array",
            "minItems": 0,
            "maxItems": 128,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            },
            "uniqueItems": true
          },
          "evidenceIds": {
            "type": "array",
            "minItems": 1,
            "maxItems": 128,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            },
            "uniqueItems": true
          },
          "historicalPatternRefs": {
            "type": "array",
            "minItems": 0,
            "maxItems": 128,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            },
            "uniqueItems": true
          },
          "summary": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2000
          },
          "expectedEffect": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2000
          },
          "actualDiffRef": {
            "type": "string",
            "minLength": 0,
            "maxLength": 640
          },
          "comparisonContract": {
            "$ref": "#/$defs/optimizationContract"
          },
          "candidateId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256
          },
          "reportId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256
          },
          "optimizationProjectId": {
            "type": "string",
            "minLength": 0,
            "maxLength": 256
          },
          "comparisonContractSha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64
          },
          "intent": {
            "$ref": "#/$defs/optimizationIntent"
          },
          "executionStatus": {
            "const": "not_started"
          },
          "diffStatus": {
            "enum": [
              "verified",
              "unverified"
            ]
          },
          "availableActions": {
            "type": "array",
            "minItems": 0,
            "maxItems": 6,
            "items": {
              "enum": [
                "run_candidate",
                "install",
                "replace",
                "apply",
                "keep_original",
                "rollback"
              ]
            }
          },
          "supportedActions": {
            "type": "array",
            "minItems": 0,
            "maxItems": 6,
            "items": {
              "enum": [
                "run_candidate",
                "install",
                "replace",
                "apply",
                "keep_original",
                "rollback"
              ]
            }
          },
          "actualDiff": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "before",
              "after",
              "unifiedDiff"
            ],
            "properties": {
              "before": {
                "type": "string",
                "minLength": 0,
                "maxLength": 100000
              },
              "after": {
                "type": "string",
                "minLength": 0,
                "maxLength": 100000
              },
              "unifiedDiff": {
                "type": "string",
                "minLength": 0,
                "maxLength": 200000
              }
            }
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "contentSha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64
          }
        }
      },
      "optimizationMetric": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "metricId",
          "kind",
          "baseline",
          "candidate",
          "delta",
          "baselineNumerator",
          "baselineDenominator",
          "candidateNumerator",
          "candidateDenominator"
        ],
        "properties": {
          "metricId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "kind": {
            "enum": [
              "quality",
              "cost"
            ]
          },
          "baseline": {
            "type": [
              "number",
              "null"
            ]
          },
          "candidate": {
            "type": [
              "number",
              "null"
            ]
          },
          "delta": {
            "type": [
              "number",
              "null"
            ]
          },
          "baselineNumerator": {
            "type": [
              "number",
              "null"
            ]
          },
          "baselineDenominator": {
            "type": [
              "number",
              "null"
            ]
          },
          "candidateNumerator": {
            "type": [
              "number",
              "null"
            ]
          },
          "candidateDenominator": {
            "type": [
              "number",
              "null"
            ]
          }
        }
      },
      "optimizationCase": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "caseId",
          "baseline",
          "candidate",
          "regressed"
        ],
        "properties": {
          "caseId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "baseline": {
            "type": [
              "number",
              "null"
            ]
          },
          "candidate": {
            "type": [
              "number",
              "null"
            ]
          },
          "regressed": {
            "type": "boolean"
          }
        }
      },
      "optimizationComparison": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "comparisonId",
          "reportId",
          "candidateId",
          "optimizationProjectId",
          "baselineTrialId",
          "candidateTrialId",
          "executionStatus",
          "effectStatus",
          "decision",
          "comparable",
          "reason",
          "pairedMetrics",
          "cases",
          "regressions",
          "usage",
          "evidenceRefs",
          "actualLoadedVersions",
          "createdAtMs",
          "contentSha256",
          "validationScope"
        ],
        "properties": {
          "comparisonId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256
          },
          "reportId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256
          },
          "candidateId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256
          },
          "optimizationProjectId": {
            "type": "string",
            "minLength": 0,
            "maxLength": 256
          },
          "baselineTrialId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256
          },
          "candidateTrialId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256
          },
          "executionStatus": {
            "enum": [
              "not_started",
              "completed",
              "failed",
              "cancelled",
              "interrupted"
            ]
          },
          "effectStatus": {
            "enum": [
              "improved",
              "neutral",
              "regressed",
              "not_run",
              "unverified"
            ]
          },
          "decision": {
            "enum": [
              "kept",
              "rejected",
              "needs_validation"
            ]
          },
          "comparable": {
            "type": "boolean"
          },
          "reason": {
            "type": "string",
            "minLength": 0,
            "maxLength": 2000
          },
          "pairedMetrics": {
            "type": "array",
            "minItems": 0,
            "maxItems": 17,
            "items": {
              "$ref": "#/$defs/optimizationMetric"
            }
          },
          "cases": {
            "type": "array",
            "minItems": 0,
            "maxItems": 128,
            "items": {
              "$ref": "#/$defs/optimizationCase"
            }
          },
          "regressions": {
            "type": "array",
            "minItems": 0,
            "maxItems": 128,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            },
            "uniqueItems": true
          },
          "usage": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "baselineCost",
              "candidateCost",
              "currency",
              "complete"
            ],
            "properties": {
              "baselineCost": {
                "type": [
                  "number",
                  "null"
                ]
              },
              "candidateCost": {
                "type": [
                  "number",
                  "null"
                ]
              },
              "currency": {
                "type": "string",
                "minLength": 0,
                "maxLength": 16
              },
              "complete": {
                "type": "boolean"
              }
            }
          },
          "evidenceRefs": {
            "type": "array",
            "minItems": 0,
            "maxItems": 1024,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            },
            "uniqueItems": true
          },
          "actualLoadedVersions": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "baseline",
              "candidate"
            ],
            "properties": {
              "baseline": {
                "type": "object",
                "additionalProperties": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 640
                }
              },
              "candidate": {
                "type": "object",
                "additionalProperties": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 640
                }
              }
            }
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "contentSha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64
          },
          "validationScope": {
            "enum": [
              "unverified",
              "frozen_local_task_fixture",
              "registered_task_execution"
            ]
          }
        }
      },
      "optimizationApplication": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "applicationId",
          "reportId",
          "candidateId",
          "comparisonId",
          "action",
          "status",
          "receiptRef",
          "targetRef",
          "versionRef",
          "createdAtMs",
          "contentSha256"
        ],
        "properties": {
          "applicationId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256
          },
          "reportId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256
          },
          "candidateId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256
          },
          "comparisonId": {
            "type": "string",
            "minLength": 0,
            "maxLength": 256
          },
          "action": {
            "enum": [
              "install",
              "replace",
              "apply",
              "keep_original",
              "rollback"
            ]
          },
          "status": {
            "enum": [
              "kept_original",
              "applied",
              "failed",
              "rolled_back",
              "interrupted"
            ]
          },
          "receiptRef": {
            "type": "string",
            "minLength": 0,
            "maxLength": 640
          },
          "targetRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "versionRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "contentSha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64
          }
        }
      },
      "optimization": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "schemaVersion",
          "candidates",
          "comparisons",
          "applications"
        ],
        "properties": {
          "schemaVersion": {
            "const": "rag-ime.trace-optimization.v1"
          },
          "candidates": {
            "type": "array",
            "minItems": 0,
            "maxItems": 128,
            "items": {
              "$ref": "#/$defs/optimizationCandidate"
            }
          },
          "comparisons": {
            "type": "array",
            "minItems": 0,
            "maxItems": 512,
            "items": {
              "$ref": "#/$defs/optimizationComparison"
            }
          },
          "applications": {
            "type": "array",
            "minItems": 0,
            "maxItems": 512,
            "items": {
              "$ref": "#/$defs/optimizationApplication"
            }
          },
          "executions": {
            "type": "array",
            "items": {
              "$ref": "#/$defs/optimizationExecution"
            }
          },
          "pendingApplications": {
            "type": "array",
            "maxItems": 512,
            "items": {
              "$ref": "#/$defs/optimizationPendingApplication"
            }
          }
        }
      },
      "distillation": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "items",
          "authority",
          "inventorySha256",
          "sourceInspectionSha256"
        ],
        "properties": {
          "items": {
            "type": "array",
            "maxItems": 24,
            "items": {
              "type": "object",
              "additionalProperties": false,
              "required": [
                "suggestionId",
                "outcome",
                "requestedOutcome",
                "title",
                "reason",
                "proposedChange",
                "evidenceIds",
                "sourceRefs",
                "existingCapabilityIds",
                "capabilityNeeds",
                "validationStatus",
                "candidateIds"
              ],
              "properties": {
                "suggestionId": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 256
                },
                "outcome": {
                  "enum": [
                    "update_existing",
                    "new_skill",
                    "new_tool",
                    "experience_only",
                    "no_change"
                  ]
                },
                "requestedOutcome": {
                  "enum": [
                    "update_existing",
                    "new_skill",
                    "new_tool",
                    "experience_only",
                    "no_change"
                  ]
                },
                "title": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 240
                },
                "reason": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 2000
                },
                "proposedChange": {
                  "type": "string",
                  "minLength": 0,
                  "maxLength": 2000
                },
                "evidenceIds": {
                  "type": "array",
                  "maxItems": 128,
                  "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 640
                  },
                  "uniqueItems": true
                },
                "sourceRefs": {
                  "type": "array",
                  "maxItems": 24,
                  "items": {
                    "type": "object",
                    "additionalProperties": false,
                    "required": [
                      "evidenceId",
                      "sourceRef",
                      "targetKeys",
                      "sourceSha256"
                    ],
                    "properties": {
                      "evidenceId": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 640
                      },
                      "sourceRef": {
                        "type": "string",
                        "minLength": 0,
                        "maxLength": 640
                      },
                      "targetKeys": {
                        "type": "array",
                        "maxItems": 12,
                        "items": {
                          "type": "string",
                          "minLength": 1,
                          "maxLength": 500
                        },
                        "uniqueItems": true
                      },
                      "sourceSha256": {
                        "type": "string",
                        "pattern": "^[a-f0-9]{64}$"
                      }
                    }
                  }
                },
                "existingCapabilityIds": {
                  "type": "array",
                  "maxItems": 128,
                  "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 640
                  },
                  "uniqueItems": true
                },
                "capabilityNeeds": {
                  "type": "array",
                  "maxItems": 32,
                  "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 160
                  },
                  "uniqueItems": true
                },
                "validationStatus": {
                  "enum": [
                    "candidate_draft",
                    "not_applicable"
                  ]
                },
                "candidateIds": {
                  "type": "array",
                  "maxItems": 128,
                  "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 640
                  },
                  "uniqueItems": true
                }
              }
            }
          },
          "authority": {
            "const": "analysis_proposal"
          },
          "inventorySha256": {
            "type": "string",
            "pattern": "^[a-f0-9]{64}$"
          },
          "sourceInspectionSha256": {
            "type": "string",
            "pattern": "^[a-f0-9]{64}$"
          }
        }
      },
      "optimizationExecution": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "requestId",
          "candidateId",
          "baselineJobId",
          "candidateJobId",
          "baselineState",
          "candidateState",
          "baselineSummary",
          "candidateSummary",
          "comparisonId",
          "createdAtMs"
        ],
        "properties": {
          "requestId": {
            "type": "string",
            "maxLength": 640
          },
          "candidateId": {
            "type": "string",
            "maxLength": 640
          },
          "baselineJobId": {
            "type": "string",
            "maxLength": 640
          },
          "candidateJobId": {
            "type": "string",
            "maxLength": 640
          },
          "baselineState": {
            "enum": [
              "queued",
              "preparing",
              "running",
              "cancelling",
              "completed",
              "failed",
              "cancelled",
              "interrupted",
              "unavailable"
            ]
          },
          "candidateState": {
            "enum": [
              "queued",
              "preparing",
              "running",
              "cancelling",
              "completed",
              "failed",
              "cancelled",
              "interrupted",
              "unavailable"
            ]
          },
          "baselineSummary": {
            "type": "string",
            "maxLength": 2000
          },
          "candidateSummary": {
            "type": "string",
            "maxLength": 2000
          },
          "comparisonId": {
            "type": "string",
            "maxLength": 640
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "target": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "targetKey",
          "kind",
          "id",
          "title",
          "traceIds",
          "sourceAvailable"
        ],
        "properties": {
          "targetKey": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500
          },
          "kind": {
            "enum": [
              "session",
              "room",
              "run"
            ]
          },
          "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "title": {
            "type": "string",
            "maxLength": 240
          },
          "traceIds": {
            "type": "array",
            "maxItems": 32,
            "uniqueItems": true,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 240
            }
          },
          "sourceAvailable": {
            "type": "boolean"
          }
        }
      },
      "repairAuthorization": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "state",
          "authorizationKind",
          "writeAuthority",
          "authorizationId",
          "findingId",
          "sourceScope",
          "sourceTraceId",
          "failureRef",
          "repairSessionId",
          "authorizedAtMs"
        ],
        "properties": {
          "state": {
            "enum": [
              "authorized",
              "declined",
              "blocked",
              "expired"
            ]
          },
          "authorizationKind": {
            "const": "repair_handoff"
          },
          "writeAuthority": {
            "enum": [
              "per_action_required",
              "model_arbitrated_full_trust",
              "auto_approved_full_trust"
            ]
          },
          "authorizationId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "findingId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "sourceScope": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "sourceTraceId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "failureRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "repairSessionId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "authorizedAtMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "comparison": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "status",
          "reason",
          "sourceStatus",
          "repairStatus",
          "sourceFingerprint",
          "repairFingerprint",
          "beforeMetrics",
          "afterMetrics",
          "deltas"
        ],
        "properties": {
          "status": {
            "enum": [
              "pending",
              "incomparable",
              "failed",
              "unknown"
            ]
          },
          "reason": {
            "type": "string",
            "maxLength": 1000
          },
          "sourceStatus": {
            "type": "string",
            "maxLength": 80
          },
          "repairStatus": {
            "type": "string",
            "maxLength": 80
          },
          "sourceFingerprint": {
            "type": "string",
            "pattern": "^$|^sha256:[a-f0-9]{64}$"
          },
          "repairFingerprint": {
            "type": "string",
            "pattern": "^$|^sha256:[a-f0-9]{64}$"
          },
          "beforeMetrics": {
            "type": "object",
            "additionalProperties": {
              "type": "number"
            }
          },
          "afterMetrics": {
            "type": "object",
            "additionalProperties": {
              "type": "number"
            }
          },
          "deltas": {
            "type": "object",
            "additionalProperties": {
              "type": "number"
            }
          }
        }
      },
      "repairVerification": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "state",
          "repairReceiptId",
          "repairTraceId",
          "evalRunId",
          "testStatus",
          "sandboxStatus",
          "sandboxedTestCount",
          "verifiedAtMs",
          "comparison"
        ],
        "properties": {
          "state": {
            "enum": [
              "pending",
              "verified",
              "failed"
            ]
          },
          "repairReceiptId": {
            "type": "string",
            "maxLength": 160
          },
          "repairTraceId": {
            "type": "string",
            "maxLength": 160
          },
          "evalRunId": {
            "type": "string",
            "maxLength": 160
          },
          "verificationReceiptId": {
            "type": "string",
            "maxLength": 160
          },
          "replayCaseId": {
            "type": "string",
            "maxLength": 160
          },
          "decision": {
            "enum": [
              "",
              "kept",
              "rejected"
            ]
          },
          "testStatus": {
            "enum": [
              "",
              "passed",
              "failed",
              "blocked"
            ]
          },
          "sandboxStatus": {
            "enum": [
              "",
              "passed",
              "not_required",
              "blocked"
            ]
          },
          "sandboxedTestCount": {
            "type": "integer",
            "minimum": 0
          },
          "verifiedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "comparison": {
            "$ref": "#/$defs/comparison"
          }
        }
      },
      "repairLifecycle": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "authorization",
          "verification"
        ],
        "properties": {
          "authorization": {
            "$ref": "#/$defs/repairAuthorization"
          },
          "verification": {
            "$ref": "#/$defs/repairVerification"
          }
        }
      },
      "optimizationPendingApplication": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "candidateId",
          "comparisonId",
          "receiptRef",
          "versionRef",
          "action",
          "status",
          "createdAtMs"
        ],
        "properties": {
          "candidateId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "comparisonId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "receiptRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "versionRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "action": {
            "enum": [
              "install",
              "replace",
              "apply",
              "rollback"
            ]
          },
          "status": {
            "enum": [
              "applying",
              "interrupted"
            ]
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      }
    }
  },
  "trace-diagnostic-result.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.trace-diagnostic-result.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "summary",
      "hardGates",
      "judgeScores",
      "findings"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.trace-diagnostic-result.v1"
      },
      "summary": {
        "type": "string",
        "minLength": 1,
        "maxLength": 4000
      },
      "hardGates": {
        "type": "array",
        "maxItems": 16,
        "items": {
          "$ref": "#/$defs/gate"
        }
      },
      "judgeScores": {
        "type": "array",
        "maxItems": 8,
        "items": {
          "$ref": "#/$defs/judge"
        }
      },
      "requirementAssessments": {
        "type": "array",
        "maxItems": 100,
        "items": {
          "$ref": "#/$defs/requirementAssessment"
        }
      },
      "causalLinks": {
        "type": "array",
        "maxItems": 120,
        "items": {
          "$ref": "#/$defs/causalLink"
        }
      },
      "findings": {
        "type": "array",
        "maxItems": 100,
        "items": {
          "$ref": "#/$defs/finding"
        }
      },
      "presentation": {
        "$ref": "#/$defs/presentation"
      }
    },
    "$defs": {
      "evidenceIds": {
        "type": "array",
        "maxItems": 128,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "minLength": 1,
          "maxLength": 640
        }
      },
      "dimensionId": {
        "enum": [
          "task_completion",
          "evidence_diagnosis",
          "tool_runtime",
          "context",
          "room_collaboration",
          "memory_rag",
          "efficiency",
          "repair_quality"
        ]
      },
      "gate": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "gateId",
          "status",
          "reason",
          "evidenceIds"
        ],
        "properties": {
          "gateId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120
          },
          "status": {
            "enum": [
              "passed",
              "failed",
              "unknown"
            ]
          },
          "reason": {
            "type": "string",
            "maxLength": 1000
          },
          "evidenceIds": {
            "$ref": "#/$defs/evidenceIds"
          }
        }
      },
      "judge": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "dimensionId",
          "score",
          "authority",
          "explanation",
          "evidenceIds"
        ],
        "properties": {
          "dimensionId": {
            "$ref": "#/$defs/dimensionId"
          },
          "score": {
            "type": [
              "integer",
              "null"
            ],
            "minimum": 0,
            "maximum": 3
          },
          "authority": {
            "const": "ai_judge_estimate"
          },
          "explanation": {
            "type": "string",
            "maxLength": 1600
          },
          "evidenceIds": {
            "$ref": "#/$defs/evidenceIds"
          }
        }
      },
      "requirementAssessment": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "requirementId",
          "status",
          "owner",
          "authority",
          "evidenceIds",
          "note"
        ],
        "properties": {
          "requirementId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "status": {
            "enum": [
              "satisfied",
              "partial",
              "unsatisfied",
              "unverified"
            ]
          },
          "owner": {
            "type": "string",
            "maxLength": 240
          },
          "authority": {
            "const": "ai_judge_estimate"
          },
          "evidenceIds": {
            "$ref": "#/$defs/evidenceIds"
          },
          "note": {
            "type": "string",
            "maxLength": 1600
          }
        }
      },
      "causalLink": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "linkId",
          "fromEvidenceId",
          "toEvidenceId",
          "relation",
          "authority",
          "confidence",
          "explanation"
        ],
        "properties": {
          "linkId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "fromEvidenceId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "toEvidenceId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "relation": {
            "enum": [
              "triggered",
              "delegated",
              "responded_to",
              "returned",
              "verified",
              "caused",
              "recovered"
            ]
          },
          "authority": {
            "const": "ai_judge_estimate"
          },
          "confidence": {
            "enum": [
              "high",
              "medium",
              "low",
              "unknown"
            ]
          },
          "explanation": {
            "type": "string",
            "maxLength": 1600
          }
        }
      },
      "finding": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "findingId",
          "dimensionId",
          "severity",
          "observation",
          "hypothesis",
          "conclusion",
          "confidence",
          "evidenceIds",
          "candidateRepair",
          "verification"
        ],
        "properties": {
          "candidateIds": {
            "type": "array",
            "maxItems": 32,
            "uniqueItems": true,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            }
          },
          "findingId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "dimensionId": {
            "$ref": "#/$defs/dimensionId"
          },
          "severity": {
            "enum": [
              "critical",
              "high",
              "medium",
              "low"
            ]
          },
          "observation": {
            "type": "string",
            "maxLength": 2000
          },
          "hypothesis": {
            "type": "string",
            "maxLength": 2000
          },
          "conclusion": {
            "type": "string",
            "maxLength": 2000
          },
          "confidence": {
            "enum": [
              "high",
              "medium",
              "low",
              "unknown"
            ]
          },
          "evidenceIds": {
            "$ref": "#/$defs/evidenceIds"
          },
          "candidateRepair": {
            "type": "string",
            "maxLength": 2000
          },
          "verification": {
            "type": "string",
            "maxLength": 2000
          }
        }
      },
      "presentation": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "headline",
          "impact",
          "primaryFindingId",
          "knownFacts",
          "evidenceGaps",
          "causalNodes",
          "expectedStageCount",
          "recordedStageReceiptEvidenceIds"
        ],
        "properties": {
          "headline": {
            "type": "string",
            "minLength": 1,
            "maxLength": 320
          },
          "impact": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000
          },
          "primaryFindingId": {
            "type": "string",
            "maxLength": 160
          },
          "knownFacts": {
            "type": "array",
            "maxItems": 12,
            "items": {
              "$ref": "#/$defs/presentationKnownFact"
            }
          },
          "evidenceGaps": {
            "type": "array",
            "maxItems": 12,
            "items": {
              "$ref": "#/$defs/presentationEvidenceGap"
            }
          },
          "causalNodes": {
            "type": "array",
            "maxItems": 16,
            "items": {
              "$ref": "#/$defs/presentationCausalNode"
            }
          },
          "expectedStageCount": {
            "type": "integer",
            "minimum": 0,
            "maximum": 32
          },
          "recordedStageReceiptEvidenceIds": {
            "$ref": "#/$defs/presentationEvidenceIds"
          },
          "failureAttribution": {
            "$ref": "#/$defs/failureAttribution"
          }
        }
      },
      "presentationEvidenceIds": {
        "type": "array",
        "maxItems": 32,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "minLength": 1,
          "maxLength": 640
        }
      },
      "presentationKnownFact": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "fact",
          "evidenceIds"
        ],
        "properties": {
          "fact": {
            "type": "string",
            "minLength": 1,
            "maxLength": 800
          },
          "evidenceIds": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "uniqueItems": true,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            }
          }
        }
      },
      "presentationEvidenceGap": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "gap",
          "consequence",
          "howToObtain"
        ],
        "properties": {
          "gap": {
            "type": "string",
            "minLength": 1,
            "maxLength": 800
          },
          "consequence": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000
          },
          "howToObtain": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000
          }
        }
      },
      "presentationCausalNode": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "label",
          "detail",
          "status",
          "evidenceIds"
        ],
        "properties": {
          "label": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "detail": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1200
          },
          "status": {
            "enum": [
              "confirmed",
              "unverified"
            ]
          },
          "evidenceIds": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "uniqueItems": true,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            }
          }
        }
      },
      "failureAttribution": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "primaryLayer",
          "summary",
          "layers"
        ],
        "properties": {
          "primaryLayer": {
            "enum": [
              "tool",
              "skill",
              "template",
              "workflow",
              "model",
              "unknown"
            ]
          },
          "summary": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1600
          },
          "layers": {
            "type": "array",
            "minItems": 5,
            "maxItems": 5,
            "items": {
              "$ref": "#/$defs/failureAttributionLayer"
            },
            "prefixItems": [
              {
                "$ref": "#/$defs/failureAttributionToolLayer"
              },
              {
                "$ref": "#/$defs/failureAttributionSkillLayer"
              },
              {
                "$ref": "#/$defs/failureAttributionTemplateLayer"
              },
              {
                "$ref": "#/$defs/failureAttributionWorkflowLayer"
              },
              {
                "$ref": "#/$defs/failureAttributionModelLayer"
              }
            ]
          }
        }
      },
      "failureAttributionEvidenceIds": {
        "type": "array",
        "maxItems": 32,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "minLength": 1,
          "maxLength": 640
        }
      },
      "failureAttributionLayer": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "layer",
          "verdict",
          "explanation",
          "evidenceIds"
        ],
        "properties": {
          "layer": {
            "enum": [
              "tool",
              "skill",
              "template",
              "workflow",
              "model"
            ]
          },
          "verdict": {
            "enum": [
              "primary",
              "contributing",
              "healthy",
              "unknown",
              "not_applicable"
            ]
          },
          "explanation": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1200
          },
          "evidenceIds": {
            "$ref": "#/$defs/failureAttributionEvidenceIds"
          }
        },
        "allOf": [
          {
            "if": {
              "properties": {
                "verdict": {
                  "enum": [
                    "primary",
                    "contributing",
                    "healthy"
                  ]
                }
              },
              "required": [
                "verdict"
              ]
            },
            "then": {
              "properties": {
                "evidenceIds": {
                  "minItems": 1
                }
              }
            }
          }
        ]
      },
      "failureAttributionToolLayer": {
        "allOf": [
          {
            "$ref": "#/$defs/failureAttributionLayer"
          },
          {
            "properties": {
              "layer": {
                "const": "tool"
              }
            }
          }
        ]
      },
      "failureAttributionSkillLayer": {
        "allOf": [
          {
            "$ref": "#/$defs/failureAttributionLayer"
          },
          {
            "properties": {
              "layer": {
                "const": "skill"
              }
            }
          }
        ]
      },
      "failureAttributionTemplateLayer": {
        "allOf": [
          {
            "$ref": "#/$defs/failureAttributionLayer"
          },
          {
            "properties": {
              "layer": {
                "const": "template"
              }
            }
          }
        ]
      },
      "failureAttributionWorkflowLayer": {
        "allOf": [
          {
            "$ref": "#/$defs/failureAttributionLayer"
          },
          {
            "properties": {
              "layer": {
                "const": "workflow"
              }
            }
          }
        ]
      },
      "failureAttributionModelLayer": {
        "allOf": [
          {
            "$ref": "#/$defs/failureAttributionLayer"
          },
          {
            "properties": {
              "layer": {
                "const": "model"
              }
            }
          }
        ]
      }
    }
  },
  "trace-envelope.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.trace-envelope.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "traceId",
      "sourceKind",
      "status",
      "binding",
      "input",
      "spans",
      "evidence",
      "artifacts",
      "createdAtMs",
      "updatedAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.trace-envelope.v1"
      },
      "traceId": {
        "type": "string",
        "minLength": 1
      },
      "sourceKind": {
        "type": "string",
        "minLength": 1
      },
      "status": {
        "enum": [
          "building",
          "completed",
          "failed",
          "cancelled"
        ]
      },
      "binding": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "sessionId": {
            "type": "string"
          },
          "turnId": {
            "type": "string"
          },
          "roomId": {
            "type": "string"
          },
          "runId": {
            "type": "string"
          },
          "sourceLoopId": {
            "type": "string"
          },
          "workItemId": {
            "type": "string"
          },
          "caseId": {
            "type": "string"
          }
        }
      },
      "parentTraceId": {
        "type": [
          "string",
          "null"
        ],
        "minLength": 1,
        "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$"
      },
      "links": {
        "type": "array",
        "maxItems": 64,
        "items": {
          "$ref": "#/$defs/traceLink"
        }
      },
      "input": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "fingerprint",
          "contentPolicy",
          "normalization"
        ],
        "properties": {
          "fingerprint": {
            "type": "string",
            "pattern": "^sha256:[a-f0-9]{64}$"
          },
          "contentPolicy": {
            "enum": [
              "hash_only",
              "redacted",
              "owner_local"
            ]
          },
          "normalization": {
            "type": "string",
            "minLength": 1
          }
        }
      },
      "spans": {
        "type": "array",
        "maxItems": 256,
        "items": {
          "$ref": "#/$defs/span"
        }
      },
      "evidence": {
        "type": "array",
        "maxItems": 2048,
        "items": {
          "$ref": "#/$defs/evidence"
        }
      },
      "artifacts": {
        "type": "array",
        "maxItems": 256,
        "items": {
          "$ref": "#/$defs/artifact"
        }
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "updatedAtMs": {
        "type": "integer",
        "minimum": 0
      }
    },
    "$defs": {
      "optimizationExecution": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "candidateId",
          "role",
          "comparisonContractSha256",
          "controls",
          "loadedVersions"
        ],
        "properties": {
          "candidateId": {
            "type": "string",
            "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$"
          },
          "role": {
            "enum": [
              "baseline",
              "candidate"
            ]
          },
          "comparisonContractSha256": {
            "type": "string",
            "pattern": "^[a-f0-9]{64}$"
          },
          "controls": {
            "type": "object",
            "maxProperties": 32,
            "propertyNames": {
              "type": "string",
              "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$"
            },
            "additionalProperties": {
              "type": "string",
              "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$"
            },
            "required": [
              "inputState",
              "evaluator",
              "qualityPolicy",
              "permissions",
              "environment"
            ]
          },
          "loadedVersions": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "tool",
              "skill",
              "prompt",
              "workflow",
              "model"
            ],
            "properties": {
              "tool": {
                "type": "string",
                "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$"
              },
              "skill": {
                "type": "string",
                "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$"
              },
              "prompt": {
                "type": "string",
                "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$"
              },
              "workflow": {
                "type": "string",
                "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$"
              },
              "model": {
                "type": "string",
                "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$"
              }
            }
          },
          "fixedContextFingerprints": {
            "type": "object",
            "maxProperties": 32,
            "propertyNames": {
              "type": "string",
              "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$"
            },
            "additionalProperties": {
              "type": "string",
              "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$"
            }
          }
        }
      },
      "span": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "spanId",
          "name",
          "parentSpanId",
          "status",
          "startedAtMs",
          "endedAtMs",
          "durationMs",
          "recorded",
          "unavailableReason",
          "metrics",
          "attributes"
        ],
        "properties": {
          "spanId": {
            "type": "string",
            "minLength": 1
          },
          "name": {
            "type": "string",
            "minLength": 1
          },
          "parentSpanId": {
            "type": [
              "string",
              "null"
            ]
          },
          "status": {
            "enum": [
              "queued",
              "running",
              "waiting",
              "completed",
              "failed",
              "cancelled",
              "info"
            ]
          },
          "startedAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "endedAtMs": {
            "type": [
              "integer",
              "null"
            ],
            "minimum": 0
          },
          "durationMs": {
            "type": [
              "integer",
              "null"
            ],
            "minimum": 0
          },
          "recorded": {
            "type": "boolean"
          },
          "unavailableReason": {
            "type": "string"
          },
          "metrics": {
            "type": "object"
          },
          "attributes": {
            "type": "object",
            "properties": {
              "traceOptimization": {
                "$ref": "#/$defs/optimizationExecution"
              }
            }
          }
        }
      },
      "evidence": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "evidenceId",
          "sourceKind",
          "sourceRef",
          "sourceLane",
          "evidenceStage",
          "disposition",
          "scores",
          "rankBefore",
          "rankAfter",
          "omissionReason"
        ],
        "properties": {
          "evidenceId": {
            "type": "string",
            "minLength": 1
          },
          "sourceKind": {
            "type": "string",
            "minLength": 1
          },
          "sourceRef": {
            "type": "string",
            "minLength": 1
          },
          "sourceLane": {
            "type": "string"
          },
          "evidenceStage": {
            "type": "string",
            "minLength": 1
          },
          "disposition": {
            "enum": [
              "included",
              "omitted",
              "filtered",
              "redacted"
            ]
          },
          "scores": {
            "type": "object",
            "additionalProperties": {
              "type": "number"
            }
          },
          "rankBefore": {
            "type": [
              "integer",
              "null"
            ],
            "minimum": 1
          },
          "rankAfter": {
            "type": [
              "integer",
              "null"
            ],
            "minimum": 1
          },
          "omissionReason": {
            "type": "string"
          }
        }
      },
      "artifact": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "artifactId",
          "kind",
          "mediaType",
          "sha256",
          "byteSize",
          "recordCount"
        ],
        "properties": {
          "artifactId": {
            "type": "string",
            "minLength": 1
          },
          "kind": {
            "type": "string",
            "minLength": 1
          },
          "mediaType": {
            "type": "string",
            "minLength": 1
          },
          "sha256": {
            "type": "string",
            "pattern": "^[a-f0-9]{64}$"
          },
          "byteSize": {
            "type": "integer",
            "minimum": 0
          },
          "recordCount": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "traceLink": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "traceId",
          "relation",
          "targetKind"
        ],
        "properties": {
          "traceId": {
            "type": "string",
            "minLength": 1,
            "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$"
          },
          "relation": {
            "enum": [
              "retry",
              "related"
            ]
          },
          "targetKind": {
            "const": "trace"
          }
        }
      }
    }
  },
  "trace-optimization.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.trace-optimization.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "candidates",
      "comparisons",
      "applications"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.trace-optimization.v1"
      },
      "candidates": {
        "type": "array",
        "minItems": 0,
        "maxItems": 128,
        "items": {
          "$ref": "#/$defs/optimizationCandidate"
        }
      },
      "comparisons": {
        "type": "array",
        "minItems": 0,
        "maxItems": 512,
        "items": {
          "$ref": "#/$defs/optimizationComparison"
        }
      },
      "applications": {
        "type": "array",
        "minItems": 0,
        "maxItems": 512,
        "items": {
          "$ref": "#/$defs/optimizationApplication"
        }
      },
      "executions": {
        "type": "array",
        "items": {
          "$ref": "#/$defs/optimizationExecution"
        }
      },
      "pendingApplications": {
        "type": "array",
        "maxItems": 512,
        "items": {
          "$ref": "#/$defs/optimizationPendingApplication"
        }
      }
    },
    "$defs": {
      "optimizationIntent": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "mode",
          "scopeMode",
          "focusAreas",
          "objective"
        ],
        "properties": {
          "mode": {
            "enum": [
              "improve",
              "distill"
            ]
          },
          "scopeMode": {
            "enum": [
              "all",
              "selected"
            ]
          },
          "focusAreas": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "items": {
              "enum": [
                "tool",
                "skill",
                "prompt",
                "workflow",
                "model"
              ],
              "type": "string"
            },
            "uniqueItems": true
          },
          "objective": {
            "type": "string",
            "minLength": 0,
            "maxLength": 2000
          }
        }
      },
      "optimizationChange": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "kind",
          "targetRef",
          "beforeVersionRef",
          "afterVersionRef"
        ],
        "properties": {
          "kind": {
            "enum": [
              "tool",
              "skill",
              "prompt",
              "workflow",
              "model"
            ]
          },
          "targetRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "beforeVersionRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "afterVersionRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          }
        }
      },
      "optimizationQualityGate": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "metricId",
          "minimum"
        ],
        "properties": {
          "metricId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "minimum": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          }
        }
      },
      "optimizationContract": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "caseSetRef",
          "caseIds",
          "controls",
          "declaredChanges",
          "qualityGates",
          "costMetric"
        ],
        "properties": {
          "caseSetRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "caseIds": {
            "type": "array",
            "minItems": 1,
            "maxItems": 128,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 240
            },
            "uniqueItems": true
          },
          "controls": {
            "type": "object",
            "additionalProperties": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            },
            "required": [
              "inputState",
              "evaluator",
              "qualityPolicy",
              "permissions",
              "environment"
            ]
          },
          "declaredChanges": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "items": {
              "$ref": "#/$defs/optimizationChange"
            }
          },
          "qualityGates": {
            "type": "array",
            "minItems": 1,
            "maxItems": 16,
            "items": {
              "$ref": "#/$defs/optimizationQualityGate"
            }
          },
          "costMetric": {
            "enum": [
              "",
              "totalCost"
            ]
          }
        }
      },
      "optimizationProposal": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "targetKind",
          "targetRef",
          "parentVersionRef",
          "candidateVersionRef",
          "findingIds",
          "evidenceIds",
          "historicalPatternRefs",
          "summary",
          "expectedEffect",
          "actualDiffRef",
          "comparisonContract"
        ],
        "properties": {
          "targetKind": {
            "enum": [
              "tool",
              "skill",
              "prompt",
              "workflow",
              "model"
            ]
          },
          "targetRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "parentVersionRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "candidateVersionRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "findingIds": {
            "type": "array",
            "minItems": 0,
            "maxItems": 128,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            },
            "uniqueItems": true
          },
          "evidenceIds": {
            "type": "array",
            "minItems": 1,
            "maxItems": 128,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            },
            "uniqueItems": true
          },
          "historicalPatternRefs": {
            "type": "array",
            "minItems": 0,
            "maxItems": 128,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            },
            "uniqueItems": true
          },
          "summary": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2000
          },
          "expectedEffect": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2000
          },
          "actualDiffRef": {
            "type": "string",
            "minLength": 0,
            "maxLength": 640
          },
          "comparisonContract": {
            "$ref": "#/$defs/optimizationContract"
          }
        }
      },
      "optimizationCandidate": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "targetKind",
          "targetRef",
          "parentVersionRef",
          "candidateVersionRef",
          "findingIds",
          "evidenceIds",
          "historicalPatternRefs",
          "summary",
          "expectedEffect",
          "actualDiffRef",
          "comparisonContract",
          "candidateId",
          "reportId",
          "optimizationProjectId",
          "comparisonContractSha256",
          "intent",
          "executionStatus",
          "diffStatus",
          "availableActions",
          "supportedActions",
          "createdAtMs",
          "contentSha256"
        ],
        "properties": {
          "targetKind": {
            "enum": [
              "tool",
              "skill",
              "prompt",
              "workflow",
              "model"
            ]
          },
          "targetRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "parentVersionRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "candidateVersionRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "findingIds": {
            "type": "array",
            "minItems": 0,
            "maxItems": 128,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            },
            "uniqueItems": true
          },
          "evidenceIds": {
            "type": "array",
            "minItems": 1,
            "maxItems": 128,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            },
            "uniqueItems": true
          },
          "historicalPatternRefs": {
            "type": "array",
            "minItems": 0,
            "maxItems": 128,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            },
            "uniqueItems": true
          },
          "summary": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2000
          },
          "expectedEffect": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2000
          },
          "actualDiffRef": {
            "type": "string",
            "minLength": 0,
            "maxLength": 640
          },
          "comparisonContract": {
            "$ref": "#/$defs/optimizationContract"
          },
          "candidateId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256
          },
          "reportId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256
          },
          "optimizationProjectId": {
            "type": "string",
            "minLength": 0,
            "maxLength": 256
          },
          "comparisonContractSha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64
          },
          "intent": {
            "$ref": "#/$defs/optimizationIntent"
          },
          "executionStatus": {
            "const": "not_started"
          },
          "diffStatus": {
            "enum": [
              "verified",
              "unverified"
            ]
          },
          "availableActions": {
            "type": "array",
            "minItems": 0,
            "maxItems": 6,
            "items": {
              "enum": [
                "run_candidate",
                "install",
                "replace",
                "apply",
                "keep_original",
                "rollback"
              ]
            }
          },
          "supportedActions": {
            "type": "array",
            "minItems": 0,
            "maxItems": 6,
            "items": {
              "enum": [
                "run_candidate",
                "install",
                "replace",
                "apply",
                "keep_original",
                "rollback"
              ]
            }
          },
          "actualDiff": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "before",
              "after",
              "unifiedDiff"
            ],
            "properties": {
              "before": {
                "type": "string",
                "minLength": 0,
                "maxLength": 100000
              },
              "after": {
                "type": "string",
                "minLength": 0,
                "maxLength": 100000
              },
              "unifiedDiff": {
                "type": "string",
                "minLength": 0,
                "maxLength": 200000
              }
            }
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "contentSha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64
          }
        }
      },
      "optimizationMetric": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "metricId",
          "kind",
          "baseline",
          "candidate",
          "delta",
          "baselineNumerator",
          "baselineDenominator",
          "candidateNumerator",
          "candidateDenominator"
        ],
        "properties": {
          "metricId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "kind": {
            "enum": [
              "quality",
              "cost"
            ]
          },
          "baseline": {
            "type": [
              "number",
              "null"
            ]
          },
          "candidate": {
            "type": [
              "number",
              "null"
            ]
          },
          "delta": {
            "type": [
              "number",
              "null"
            ]
          },
          "baselineNumerator": {
            "type": [
              "number",
              "null"
            ]
          },
          "baselineDenominator": {
            "type": [
              "number",
              "null"
            ]
          },
          "candidateNumerator": {
            "type": [
              "number",
              "null"
            ]
          },
          "candidateDenominator": {
            "type": [
              "number",
              "null"
            ]
          }
        }
      },
      "optimizationCase": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "caseId",
          "baseline",
          "candidate",
          "regressed"
        ],
        "properties": {
          "caseId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "baseline": {
            "type": [
              "number",
              "null"
            ]
          },
          "candidate": {
            "type": [
              "number",
              "null"
            ]
          },
          "regressed": {
            "type": "boolean"
          }
        }
      },
      "optimizationComparison": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "comparisonId",
          "reportId",
          "candidateId",
          "optimizationProjectId",
          "baselineTrialId",
          "candidateTrialId",
          "executionStatus",
          "effectStatus",
          "decision",
          "comparable",
          "reason",
          "pairedMetrics",
          "cases",
          "regressions",
          "usage",
          "evidenceRefs",
          "actualLoadedVersions",
          "createdAtMs",
          "contentSha256",
          "validationScope"
        ],
        "properties": {
          "comparisonId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256
          },
          "reportId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256
          },
          "candidateId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256
          },
          "optimizationProjectId": {
            "type": "string",
            "minLength": 0,
            "maxLength": 256
          },
          "baselineTrialId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256
          },
          "candidateTrialId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256
          },
          "executionStatus": {
            "enum": [
              "not_started",
              "completed",
              "failed",
              "cancelled",
              "interrupted"
            ]
          },
          "effectStatus": {
            "enum": [
              "improved",
              "neutral",
              "regressed",
              "not_run",
              "unverified"
            ]
          },
          "decision": {
            "enum": [
              "kept",
              "rejected",
              "needs_validation"
            ]
          },
          "comparable": {
            "type": "boolean"
          },
          "reason": {
            "type": "string",
            "minLength": 0,
            "maxLength": 2000
          },
          "pairedMetrics": {
            "type": "array",
            "minItems": 0,
            "maxItems": 17,
            "items": {
              "$ref": "#/$defs/optimizationMetric"
            }
          },
          "cases": {
            "type": "array",
            "minItems": 0,
            "maxItems": 128,
            "items": {
              "$ref": "#/$defs/optimizationCase"
            }
          },
          "regressions": {
            "type": "array",
            "minItems": 0,
            "maxItems": 128,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            },
            "uniqueItems": true
          },
          "usage": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "baselineCost",
              "candidateCost",
              "currency",
              "complete"
            ],
            "properties": {
              "baselineCost": {
                "type": [
                  "number",
                  "null"
                ]
              },
              "candidateCost": {
                "type": [
                  "number",
                  "null"
                ]
              },
              "currency": {
                "type": "string",
                "minLength": 0,
                "maxLength": 16
              },
              "complete": {
                "type": "boolean"
              }
            }
          },
          "evidenceRefs": {
            "type": "array",
            "minItems": 0,
            "maxItems": 1024,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 640
            },
            "uniqueItems": true
          },
          "actualLoadedVersions": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "baseline",
              "candidate"
            ],
            "properties": {
              "baseline": {
                "type": "object",
                "additionalProperties": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 640
                }
              },
              "candidate": {
                "type": "object",
                "additionalProperties": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 640
                }
              }
            }
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "contentSha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64
          },
          "validationScope": {
            "enum": [
              "unverified",
              "frozen_local_task_fixture",
              "registered_task_execution"
            ]
          }
        }
      },
      "optimizationApplication": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "applicationId",
          "reportId",
          "candidateId",
          "comparisonId",
          "action",
          "status",
          "receiptRef",
          "targetRef",
          "versionRef",
          "createdAtMs",
          "contentSha256"
        ],
        "properties": {
          "applicationId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256
          },
          "reportId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256
          },
          "candidateId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256
          },
          "comparisonId": {
            "type": "string",
            "minLength": 0,
            "maxLength": 256
          },
          "action": {
            "enum": [
              "install",
              "replace",
              "apply",
              "keep_original",
              "rollback"
            ]
          },
          "status": {
            "enum": [
              "kept_original",
              "applied",
              "failed",
              "rolled_back",
              "interrupted"
            ]
          },
          "receiptRef": {
            "type": "string",
            "minLength": 0,
            "maxLength": 640
          },
          "targetRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "versionRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "contentSha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64
          }
        }
      },
      "optimizationExecution": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "requestId",
          "candidateId",
          "baselineJobId",
          "candidateJobId",
          "baselineState",
          "candidateState",
          "baselineSummary",
          "candidateSummary",
          "comparisonId",
          "createdAtMs"
        ],
        "properties": {
          "requestId": {
            "type": "string",
            "maxLength": 640
          },
          "candidateId": {
            "type": "string",
            "maxLength": 640
          },
          "baselineJobId": {
            "type": "string",
            "maxLength": 640
          },
          "candidateJobId": {
            "type": "string",
            "maxLength": 640
          },
          "baselineState": {
            "enum": [
              "queued",
              "preparing",
              "running",
              "cancelling",
              "completed",
              "failed",
              "cancelled",
              "interrupted",
              "unavailable"
            ]
          },
          "candidateState": {
            "enum": [
              "queued",
              "preparing",
              "running",
              "cancelling",
              "completed",
              "failed",
              "cancelled",
              "interrupted",
              "unavailable"
            ]
          },
          "baselineSummary": {
            "type": "string",
            "maxLength": 2000
          },
          "candidateSummary": {
            "type": "string",
            "maxLength": 2000
          },
          "comparisonId": {
            "type": "string",
            "maxLength": 640
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "optimizationPendingApplication": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "candidateId",
          "comparisonId",
          "receiptRef",
          "versionRef",
          "action",
          "status",
          "createdAtMs"
        ],
        "properties": {
          "candidateId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "comparisonId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "receiptRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "versionRef": {
            "type": "string",
            "minLength": 1,
            "maxLength": 640
          },
          "action": {
            "enum": [
              "install",
              "replace",
              "apply",
              "rollback"
            ]
          },
          "status": {
            "enum": [
              "applying",
              "interrupted"
            ]
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          }
        }
      }
    }
  },
  "trace-repair-receipt.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.trace-repair-receipt.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "repairReceiptId",
      "sourceScope",
      "sourceTraceId",
      "failureRef",
      "changeReceiptId",
      "testEvidenceId",
      "testStatus",
      "sandboxStatus",
      "sandboxedTestCount",
      "repairTraceId",
      "repairSessionId",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.trace-repair-receipt.v1"
      },
      "repairReceiptId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "sourceScope": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "sourceTraceId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "failureRef": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "changeReceiptId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "testEvidenceId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "testStatus": {
        "const": "passed"
      },
      "sandboxStatus": {
        "enum": [
          "passed",
          "not_required"
        ]
      },
      "sandboxedTestCount": {
        "type": "integer",
        "minimum": 0
      },
      "repairTraceId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "repairSessionId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      }
    }
  },
  "trace-replay-case.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.trace-replay-case.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "replayCaseId",
      "sourceScope",
      "failureRef",
      "sourceTraceId",
      "baselineEvalRunId",
      "baselineSandboxRunId",
      "replayCohort",
      "successCriterion",
      "baselineMetricValue",
      "rollbackTarget",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.trace-replay-case.v1"
      },
      "replayCaseId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "sourceScope": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "failureRef": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "sourceTraceId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "baselineEvalRunId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "baselineSandboxRunId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "replayCohort": {
        "$ref": "#/$defs/replayCohort"
      },
      "successCriterion": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "metric",
          "threshold",
          "direction"
        ],
        "properties": {
          "metric": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80
          },
          "threshold": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          },
          "direction": {
            "const": "at_least"
          }
        }
      },
      "baselineMetricValue": {
        "type": "number",
        "minimum": 0,
        "maximum": 1
      },
      "rollbackTarget": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      }
    },
    "$defs": {
      "replayCohort": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "suiteId",
          "suiteRevision",
          "caseId",
          "inputFingerprint",
          "environmentFingerprint",
          "configFingerprint",
          "modelProfileFingerprint",
          "toolProfileFingerprint",
          "skillProfileFingerprint"
        ],
        "properties": {
          "suiteId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "suiteRevision": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "caseId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "inputFingerprint": {
            "type": "string",
            "pattern": "^sha256:[a-f0-9]{64}$"
          },
          "environmentFingerprint": {
            "type": "string",
            "pattern": "^sha256:[a-f0-9]{64}$"
          },
          "configFingerprint": {
            "type": "string",
            "pattern": "^sha256:[a-f0-9]{64}$"
          },
          "modelProfileFingerprint": {
            "type": "string",
            "pattern": "^sha256:[a-f0-9]{64}$"
          },
          "toolProfileFingerprint": {
            "type": "string",
            "pattern": "^sha256:[a-f0-9]{64}$"
          },
          "skillProfileFingerprint": {
            "type": "string",
            "pattern": "^sha256:[a-f0-9]{64}$"
          }
        }
      }
    }
  },
  "trace-verification-receipt.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.trace-verification-receipt.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "verificationReceiptId",
      "replayCaseId",
      "repairReceiptId",
      "sourceTraceId",
      "repairTraceId",
      "baselineEvalRunId",
      "repairEvalRunId",
      "baselineSandboxRunId",
      "repairSandboxRunId",
      "regressionEvalRunIds",
      "replayCohort",
      "successCriterion",
      "repairPassed",
      "regression",
      "comparison",
      "efficiency",
      "decision",
      "rollbackTarget",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.trace-verification-receipt.v1"
      },
      "verificationReceiptId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "replayCaseId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "repairReceiptId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "sourceTraceId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "repairTraceId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "baselineEvalRunId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "repairEvalRunId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "baselineSandboxRunId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "repairSandboxRunId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "regressionEvalRunIds": {
        "type": "array",
        "minItems": 1,
        "maxItems": 256,
        "uniqueItems": true,
        "items": {
          "type": "string",
          "minLength": 1,
          "maxLength": 160
        }
      },
      "replayCohort": {
        "$ref": "#/$defs/replayCohort"
      },
      "successCriterion": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "metric",
          "threshold",
          "direction"
        ],
        "properties": {
          "metric": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80
          },
          "threshold": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          },
          "direction": {
            "const": "at_least"
          }
        }
      },
      "repairPassed": {
        "type": "boolean"
      },
      "regression": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "count",
          "passed",
          "failedEvalRunIds"
        ],
        "properties": {
          "count": {
            "type": "integer",
            "minimum": 1
          },
          "passed": {
            "type": "boolean"
          },
          "failedEvalRunIds": {
            "type": "array",
            "uniqueItems": true,
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 160
            }
          }
        }
      },
      "comparison": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "status",
          "metric",
          "before",
          "after",
          "absoluteDelta",
          "relativeDelta"
        ],
        "properties": {
          "status": {
            "const": "available"
          },
          "metric": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80
          },
          "before": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          },
          "after": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          },
          "absoluteDelta": {
            "type": "number",
            "minimum": -1,
            "maximum": 1
          },
          "relativeDelta": {
            "type": [
              "number",
              "null"
            ]
          }
        }
      },
      "efficiency": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "latencyMs",
          "totalTokens"
        ],
        "properties": {
          "latencyMs": {
            "$ref": "#/$defs/nullableDelta"
          },
          "totalTokens": {
            "$ref": "#/$defs/nullableDelta"
          }
        }
      },
      "decision": {
        "enum": [
          "kept",
          "rejected"
        ]
      },
      "rollbackTarget": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      }
    },
    "$defs": {
      "replayCohort": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "suiteId",
          "suiteRevision",
          "caseId",
          "inputFingerprint",
          "environmentFingerprint",
          "configFingerprint",
          "modelProfileFingerprint",
          "toolProfileFingerprint",
          "skillProfileFingerprint"
        ],
        "properties": {
          "suiteId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "suiteRevision": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "caseId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          },
          "inputFingerprint": {
            "type": "string",
            "pattern": "^sha256:[a-f0-9]{64}$"
          },
          "environmentFingerprint": {
            "type": "string",
            "pattern": "^sha256:[a-f0-9]{64}$"
          },
          "configFingerprint": {
            "type": "string",
            "pattern": "^sha256:[a-f0-9]{64}$"
          },
          "modelProfileFingerprint": {
            "type": "string",
            "pattern": "^sha256:[a-f0-9]{64}$"
          },
          "toolProfileFingerprint": {
            "type": "string",
            "pattern": "^sha256:[a-f0-9]{64}$"
          },
          "skillProfileFingerprint": {
            "type": "string",
            "pattern": "^sha256:[a-f0-9]{64}$"
          }
        }
      },
      "nullableDelta": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "before",
          "after",
          "delta"
        ],
        "properties": {
          "before": {
            "type": [
              "integer",
              "null"
            ]
          },
          "after": {
            "type": [
              "integer",
              "null"
            ]
          },
          "delta": {
            "type": [
              "integer",
              "null"
            ]
          }
        }
      }
    }
  },
  "typed-verification-receipt.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "wisdom-weasel.typed-verification-receipt.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "receiptId",
      "rootId",
      "catalogRevisionId",
      "receiptType",
      "sourceCommit",
      "environment",
      "commandOrAction",
      "exitStatus",
      "outputHash",
      "artifactHash",
      "verifier",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "const": "wisdom-weasel.typed-verification-receipt.v1"
      },
      "receiptId": {
        "type": "string",
        "minLength": 1
      },
      "rootId": {
        "type": "string",
        "minLength": 1
      },
      "catalogRevisionId": {
        "type": "string",
        "minLength": 1
      },
      "receiptType": {
        "enum": [
          "test",
          "build",
          "install",
          "evidence"
        ]
      },
      "sourceCommit": {
        "type": "string",
        "minLength": 1
      },
      "environment": {
        "type": "string",
        "minLength": 1
      },
      "commandOrAction": {
        "type": "string",
        "minLength": 1
      },
      "exitStatus": {
        "type": "integer"
      },
      "outputHash": {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$"
      },
      "artifactHash": {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$"
      },
      "verifier": {
        "type": "string",
        "minLength": 1
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      }
    }
  },
  "user-memory-draft.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.user-memory-draft.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "draftId",
      "status",
      "project",
      "roleId",
      "sourceDigestId",
      "sourceEvidenceIds",
      "candidates",
      "policy",
      "createdAtMs"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.user-memory-draft.v1"
      },
      "draftId": {
        "type": "string",
        "minLength": 1
      },
      "status": {
        "type": "string",
        "const": "review_required"
      },
      "project": {
        "type": "string"
      },
      "roleId": {
        "type": "string",
        "minLength": 1
      },
      "sourceDigestId": {
        "type": "string",
        "minLength": 1
      },
      "sourceEvidenceIds": {
        "type": "array",
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "candidates": {
        "type": "array",
        "items": {
          "type": "object"
        }
      },
      "policy": {
        "type": "object",
        "required": [
          "rawDialoguePromotion",
          "defaultApply"
        ],
        "properties": {
          "rawDialoguePromotion": {
            "type": "string",
            "const": "forbidden"
          },
          "defaultApply": {
            "type": "boolean",
            "const": false
          }
        }
      },
      "createdAtMs": {
        "type": "integer",
        "minimum": 0
      }
    }
  },
  "vertical-agent-self-test-suite.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.vertical-agent-self-test-suite.v1",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "schemaVersion",
      "status",
      "totalCount",
      "passedCount",
      "failedCount",
      "results",
      "failures"
    ],
    "properties": {
      "schemaVersion": {
        "const": "rag-ime.vertical-agent-self-test-suite.v1"
      },
      "status": {
        "enum": [
          "completed",
          "failed"
        ]
      },
      "totalCount": {
        "type": "integer",
        "minimum": 1
      },
      "passedCount": {
        "type": "integer",
        "minimum": 0
      },
      "failedCount": {
        "type": "integer",
        "minimum": 0
      },
      "results": {
        "type": "array",
        "items": {
          "$ref": "#/$defs/result"
        }
      },
      "failures": {
        "type": "array",
        "items": {
          "$ref": "#/$defs/failure"
        }
      }
    },
    "$defs": {
      "result": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "appId",
          "fixtureId",
          "traceId",
          "evalRunId",
          "sandboxRunId",
          "metrics",
          "providerCalls",
          "productionWriteBlocked",
          "status"
        ],
        "properties": {
          "appId": {
            "type": "string",
            "minLength": 1
          },
          "fixtureId": {
            "type": "string",
            "minLength": 1
          },
          "traceId": {
            "type": "string",
            "minLength": 1
          },
          "evalRunId": {
            "type": "string",
            "minLength": 1
          },
          "sandboxRunId": {
            "type": "string",
            "minLength": 1
          },
          "metrics": {
            "type": "object",
            "additionalProperties": {
              "type": "number"
            }
          },
          "providerCalls": {
            "const": 0
          },
          "productionWriteBlocked": {
            "const": true
          },
          "status": {
            "const": "passed"
          }
        }
      },
      "failure": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "appId",
          "status",
          "errorCode",
          "errorFingerprint"
        ],
        "properties": {
          "appId": {
            "type": "string",
            "minLength": 1
          },
          "status": {
            "const": "failed"
          },
          "errorCode": {
            "type": "string",
            "minLength": 1
          },
          "errorFingerprint": {
            "type": "string",
            "pattern": "^sha256:[0-9a-f]{64}$"
          }
        }
      }
    }
  },
  "work-document-command.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.work-document-command.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "ok",
      "operation",
      "document"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.work-document-command.v1"
      },
      "ok": {
        "type": "boolean",
        "const": true
      },
      "operation": {
        "type": "string",
        "enum": [
          "register",
          "archive",
          "repair",
          "reopen",
          "erase-preview",
          "erase"
        ]
      },
      "document": {
        "anyOf": [
          {
            "$ref": "#/$defs/document"
          },
          {
            "type": "null"
          }
        ]
      },
      "receipt": {
        "$ref": "#/$defs/receipt"
      },
      "approval": {
        "type": "object"
      },
      "payloadSha256": {
        "type": "string",
        "pattern": "^[a-f0-9]{64}$"
      }
    },
    "additionalProperties": false,
    "allOf": [
      {
        "if": {
          "type": "object",
          "properties": {
            "operation": {
              "const": "erase-preview"
            }
          }
        },
        "then": {
          "required": [
            "approval",
            "payloadSha256"
          ]
        },
        "else": {
          "required": [
            "receipt"
          ]
        }
      },
      {
        "if": {
          "type": "object",
          "properties": {
            "operation": {
              "const": "erase"
            }
          }
        },
        "then": {
          "properties": {
            "document": {
              "type": "null"
            }
          }
        },
        "else": {
          "properties": {
            "document": {
              "$ref": "#/$defs/document"
            }
          }
        }
      }
    ],
    "$defs": {
      "receipt": {
        "type": "object",
        "required": [
          "receiptId",
          "operation",
          "status",
          "idempotent",
          "createdAtMs"
        ],
        "properties": {
          "receiptId": {
            "type": "string",
            "pattern": "^workdoc-receipt:[a-f0-9]{32}$"
          },
          "operation": {
            "type": "string",
            "enum": [
              "register",
              "archive",
              "repair",
              "reopen",
              "erase"
            ]
          },
          "status": {
            "type": "string",
            "enum": [
              "accepted",
              "applied",
              "failed"
            ]
          },
          "idempotent": {
            "type": "boolean"
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          }
        },
        "additionalProperties": false
      },
      "document": {
        "type": "object",
        "required": [
          "documentId",
          "authorityKind",
          "authorityId",
          "authorityRevision",
          "authorityKey",
          "documentRevision",
          "contentSha256",
          "workspaceRoot",
          "path",
          "activePath",
          "archivePath",
          "state",
          "title",
          "terminalReceiptId",
          "error",
          "createdAtMs",
          "updatedAtMs"
        ],
        "properties": {
          "documentId": {
            "type": "string",
            "pattern": "^workdoc_[a-f0-9]{32}$"
          },
          "authorityKind": {
            "type": "string",
            "enum": [
              "session_todo",
              "session_goal",
              "room_work_item"
            ]
          },
          "authorityId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "authorityRevision": {
            "type": "integer",
            "minimum": 0
          },
          "authorityKey": {
            "type": "string",
            "minLength": 1,
            "maxLength": 320
          },
          "documentRevision": {
            "type": "integer",
            "minimum": 1
          },
          "contentSha256": {
            "type": "string",
            "pattern": "^[a-f0-9]{64}$"
          },
          "workspaceRoot": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4096
          },
          "path": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000
          },
          "activePath": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000
          },
          "archivePath": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000
          },
          "state": {
            "type": "string",
            "enum": [
              "active",
              "archive_pending",
              "archived",
              "reopen_pending",
              "error"
            ]
          },
          "title": {
            "type": "string",
            "maxLength": 240
          },
          "terminalReceiptId": {
            "type": "string",
            "maxLength": 240
          },
          "error": {
            "type": "string",
            "maxLength": 500
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "updatedAtMs": {
            "type": "integer",
            "minimum": 0
          }
        },
        "additionalProperties": false
      }
    }
  },
  "work-document-context.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.work-document-context.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "items"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.work-document-context.v1"
      },
      "items": {
        "type": "array",
        "maxItems": 200,
        "items": {
          "type": "object",
          "required": [
            "documentId",
            "title",
            "path",
            "contentSha256",
            "authorityKey"
          ],
          "properties": {
            "documentId": {
              "type": "string",
              "pattern": "^workdoc_[a-f0-9]{32}$"
            },
            "title": {
              "type": "string",
              "maxLength": 240
            },
            "path": {
              "type": "string",
              "minLength": 1,
              "maxLength": 1000
            },
            "contentSha256": {
              "type": "string",
              "pattern": "^[a-f0-9]{64}$"
            },
            "authorityKey": {
              "type": "string",
              "minLength": 1,
              "maxLength": 320
            }
          },
          "additionalProperties": false
        }
      }
    },
    "additionalProperties": false
  },
  "work-document-detail.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.work-document-detail.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "document",
      "reopen"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.work-document-detail.v1"
      },
      "document": {
        "$ref": "#/$defs/document"
      },
      "reopen": {
        "type": "object",
        "required": [
          "eligible",
          "authorityRevision",
          "transitionReceiptId",
          "reasonCode"
        ],
        "properties": {
          "eligible": {
            "type": "boolean"
          },
          "authorityRevision": {
            "type": "integer",
            "minimum": 0
          },
          "transitionReceiptId": {
            "type": "string",
            "maxLength": 240
          },
          "reasonCode": {
            "type": "string",
            "enum": [
              "ready",
              "document_not_archived",
              "authority_terminal",
              "authority_not_advanced",
              "authority_unavailable"
            ]
          }
        },
        "additionalProperties": false
      }
    },
    "additionalProperties": false,
    "$defs": {
      "document": {
        "type": "object",
        "required": [
          "documentId",
          "authorityKind",
          "authorityId",
          "authorityRevision",
          "authorityKey",
          "documentRevision",
          "contentSha256",
          "workspaceRoot",
          "path",
          "activePath",
          "archivePath",
          "state",
          "title",
          "terminalReceiptId",
          "error",
          "createdAtMs",
          "updatedAtMs"
        ],
        "properties": {
          "documentId": {
            "type": "string",
            "pattern": "^workdoc_[a-f0-9]{32}$"
          },
          "authorityKind": {
            "type": "string",
            "enum": [
              "session_todo",
              "session_goal",
              "room_work_item"
            ]
          },
          "authorityId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "authorityRevision": {
            "type": "integer",
            "minimum": 0
          },
          "authorityKey": {
            "type": "string",
            "minLength": 1,
            "maxLength": 320
          },
          "documentRevision": {
            "type": "integer",
            "minimum": 1
          },
          "contentSha256": {
            "type": "string",
            "pattern": "^[a-f0-9]{64}$"
          },
          "workspaceRoot": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4096
          },
          "path": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000
          },
          "activePath": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000
          },
          "archivePath": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000
          },
          "state": {
            "type": "string",
            "enum": [
              "active",
              "archive_pending",
              "archived",
              "reopen_pending",
              "error"
            ]
          },
          "title": {
            "type": "string",
            "maxLength": 240
          },
          "terminalReceiptId": {
            "type": "string",
            "maxLength": 240
          },
          "error": {
            "type": "string",
            "maxLength": 500
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "updatedAtMs": {
            "type": "integer",
            "minimum": 0
          }
        },
        "additionalProperties": false
      }
    }
  },
  "work-document-list.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.work-document-list.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "items",
      "total"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.work-document-list.v1"
      },
      "items": {
        "type": "array",
        "maxItems": 200,
        "items": {
          "$ref": "#/$defs/document"
        }
      },
      "total": {
        "type": "integer",
        "minimum": 0
      }
    },
    "additionalProperties": false,
    "$defs": {
      "document": {
        "type": "object",
        "required": [
          "documentId",
          "authorityKind",
          "authorityId",
          "authorityRevision",
          "authorityKey",
          "documentRevision",
          "contentSha256",
          "workspaceRoot",
          "path",
          "activePath",
          "archivePath",
          "state",
          "title",
          "terminalReceiptId",
          "error",
          "createdAtMs",
          "updatedAtMs"
        ],
        "properties": {
          "documentId": {
            "type": "string",
            "pattern": "^workdoc_[a-f0-9]{32}$"
          },
          "authorityKind": {
            "type": "string",
            "enum": [
              "session_todo",
              "session_goal",
              "room_work_item"
            ]
          },
          "authorityId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240
          },
          "authorityRevision": {
            "type": "integer",
            "minimum": 0
          },
          "authorityKey": {
            "type": "string",
            "minLength": 1,
            "maxLength": 320
          },
          "documentRevision": {
            "type": "integer",
            "minimum": 1
          },
          "contentSha256": {
            "type": "string",
            "pattern": "^[a-f0-9]{64}$"
          },
          "workspaceRoot": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4096
          },
          "path": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000
          },
          "activePath": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000
          },
          "archivePath": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000
          },
          "state": {
            "type": "string",
            "enum": [
              "active",
              "archive_pending",
              "archived",
              "reopen_pending",
              "error"
            ]
          },
          "title": {
            "type": "string",
            "maxLength": 240
          },
          "terminalReceiptId": {
            "type": "string",
            "maxLength": 240
          },
          "error": {
            "type": "string",
            "maxLength": 500
          },
          "createdAtMs": {
            "type": "integer",
            "minimum": 0
          },
          "updatedAtMs": {
            "type": "integer",
            "minimum": 0
          }
        },
        "additionalProperties": false
      }
    }
  },
  "workspace-lsp-mutation-receipt.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.workspace-lsp-mutation-receipt.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "mutationApplied",
      "summary",
      "operation",
      "root",
      "server",
      "changedFiles",
      "undoAvailable"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.workspace-lsp-mutation-receipt.v1"
      },
      "mutationApplied": {
        "type": "boolean",
        "const": true
      },
      "summary": {
        "type": "string",
        "minLength": 1,
        "maxLength": 1000
      },
      "operation": {
        "type": "string",
        "enum": [
          "rename",
          "code_action_apply"
        ]
      },
      "root": {
        "type": "string",
        "minLength": 1,
        "maxLength": 4096
      },
      "server": {
        "type": "string",
        "minLength": 1,
        "maxLength": 120
      },
      "referencesEvidence": {
        "type": "object",
        "required": [
          "root",
          "path",
          "relativePath",
          "line",
          "column",
          "server",
          "resourceRevision",
          "preimageSha256",
          "count",
          "truncated",
          "items"
        ],
        "properties": {
          "root": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4096
          },
          "path": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4096
          },
          "relativePath": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4096
          },
          "line": {
            "type": "integer",
            "minimum": 1
          },
          "column": {
            "type": "integer",
            "minimum": 1
          },
          "server": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120
          },
          "resourceRevision": {
            "type": "string",
            "pattern": "^sha256:[a-f0-9]{64}$"
          },
          "preimageSha256": {
            "type": "string",
            "pattern": "^[a-f0-9]{64}$"
          },
          "count": {
            "type": "integer",
            "minimum": 0,
            "maximum": 64
          },
          "truncated": {
            "type": "boolean"
          },
          "items": {
            "type": "array",
            "maxItems": 64,
            "items": {
              "type": "object",
              "required": [
                "path",
                "relativePath",
                "line",
                "column",
                "endLine",
                "endColumn"
              ],
              "properties": {
                "path": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 4096
                },
                "relativePath": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 4096
                },
                "line": {
                  "type": "integer",
                  "minimum": 1
                },
                "column": {
                  "type": "integer",
                  "minimum": 1
                },
                "endLine": {
                  "type": "integer",
                  "minimum": 1
                },
                "endColumn": {
                  "type": "integer",
                  "minimum": 1
                }
              },
              "additionalProperties": false
            }
          }
        },
        "additionalProperties": false
      },
      "changedFiles": {
        "type": "array",
        "minItems": 1,
        "maxItems": 16,
        "items": {
          "type": "object",
          "required": [
            "path",
            "preimageSha256",
            "postimageSha256"
          ],
          "properties": {
            "path": {
              "type": "string",
              "minLength": 1,
              "maxLength": 4096
            },
            "preimageSha256": {
              "type": "string",
              "pattern": "^[a-f0-9]{64}$"
            },
            "postimageSha256": {
              "type": "string",
              "pattern": "^[a-f0-9]{64}$"
            }
          },
          "additionalProperties": false
        }
      },
      "undoAvailable": {
        "type": "boolean",
        "const": false
      }
    },
    "if": {
      "properties": {
        "operation": {
          "const": "rename"
        }
      }
    },
    "then": {
      "required": [
        "referencesEvidence"
      ]
    },
    "additionalProperties": false
  },
  "workspace-lsp-result.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.workspace-lsp-result.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "summary",
      "operation",
      "root",
      "server"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.workspace-lsp-result.v1"
      },
      "summary": {
        "type": "string",
        "minLength": 1,
        "maxLength": 1000
      },
      "operation": {
        "type": "string",
        "enum": [
          "symbols",
          "hover",
          "definition",
          "references",
          "diagnostics"
        ]
      },
      "root": {
        "type": "string",
        "minLength": 1,
        "maxLength": 4096
      },
      "server": {
        "type": "string",
        "maxLength": 1000
      },
      "items": {
        "type": "array",
        "maxItems": 200,
        "items": {
          "type": "object"
        }
      },
      "content": {
        "type": "string",
        "maxLength": 16384
      },
      "range": {
        "type": [
          "object",
          "null"
        ],
        "properties": {
          "line": {
            "type": "integer",
            "minimum": 1
          },
          "column": {
            "type": "integer",
            "minimum": 1
          },
          "endLine": {
            "type": "integer",
            "minimum": 1
          },
          "endColumn": {
            "type": "integer",
            "minimum": 1
          }
        },
        "required": [
          "line",
          "column",
          "endLine",
          "endColumn"
        ],
        "additionalProperties": false
      },
      "truncated": {
        "type": "boolean"
      }
    },
    "additionalProperties": false
  },
  "workspace-lsp-status.v1": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "rag-ime.contract.workspace-lsp-status.v1",
    "type": "object",
    "required": [
      "schemaVersion",
      "runtimeInstanceId",
      "runtimeEpoch",
      "observedAtMs",
      "heartbeatExpiresAtMs",
      "current",
      "summary",
      "state",
      "roots"
    ],
    "properties": {
      "schemaVersion": {
        "type": "string",
        "const": "rag-ime.workspace-lsp-status.v1"
      },
      "runtimeInstanceId": {
        "type": "string",
        "pattern": "^workspace-lsp-[0-9a-f]{32}$"
      },
      "runtimeEpoch": {
        "type": "integer",
        "minimum": 0
      },
      "observedAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "heartbeatExpiresAtMs": {
        "type": "integer",
        "minimum": 0
      },
      "current": {
        "type": "boolean"
      },
      "summary": {
        "type": "string",
        "minLength": 1,
        "maxLength": 1000
      },
      "state": {
        "type": "string",
        "enum": [
          "ready",
          "available",
          "degraded",
          "unavailable"
        ]
      },
      "roots": {
        "type": "array",
        "maxItems": 64,
        "items": {
          "type": "object",
          "required": [
            "root",
            "state",
            "servers"
          ],
          "properties": {
            "root": {
              "type": "string",
              "minLength": 1,
              "maxLength": 4096
            },
            "state": {
              "type": "string",
              "enum": [
                "ready",
                "available",
                "degraded",
                "unavailable"
              ]
            },
            "errorCode": {
              "type": "string",
              "maxLength": 120
            },
            "error": {
              "type": "string",
              "maxLength": 500
            },
            "servers": {
              "type": "array",
              "maxItems": 32,
              "items": {
                "type": "object",
                "required": [
                  "name",
                  "state",
                  "languageIds",
                  "fileExtensions"
                ],
                "properties": {
                  "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 120
                  },
                  "state": {
                    "type": "string",
                    "enum": [
                      "ready",
                      "available",
                      "degraded",
                      "unavailable"
                    ]
                  },
                  "languageIds": {
                    "type": "array",
                    "maxItems": 32,
                    "items": {
                      "type": "string",
                      "maxLength": 120
                    }
                  },
                  "fileExtensions": {
                    "type": "array",
                    "maxItems": 64,
                    "items": {
                      "type": "string",
                      "maxLength": 32
                    }
                  },
                  "errorCode": {
                    "type": "string",
                    "maxLength": 120
                  },
                  "error": {
                    "type": "string",
                    "maxLength": 500
                  }
                },
                "additionalProperties": false
              }
            }
          },
          "additionalProperties": false
        }
      }
    },
    "additionalProperties": false
  },
} as const;

export type ContractName = keyof typeof contractSchemas;

export const contractSchemaIds = Object.fromEntries(
  Object.entries(contractSchemas).map(([name, schema]) => [name, schema.$id]),
) as Record<ContractName, string>;
