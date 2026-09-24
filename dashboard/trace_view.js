(function (global) {
  "use strict";

  var FILTERS = ["all", "error", "tool", "output", "agent"];
  var VIEWS = ["tree", "swimlane"];
  var QUALITY_BADGES = ["approx", "semi", "heuristic", "orphan", "incomplete", "detached"];
  var SEARCH_FIELDS = ["name", "input", "output", "tool", "error", "agent"];
  var MAX_SEARCH_MATCHES = 500;
  var MAX_RENDERED_ROWS = 800;
  var MAX_SWIMLANE_BLOCKS = 1000;
  var KIND_LABELS = {
    turn: "TURN",
    llm: "LLM",
    tool: "TOOL",
    agent: "AGENT",
    error: "ERROR",
    other: "SPAN"
  };

  function firstDefined(object, keys) {
    if (!object || typeof object !== "object") return undefined;
    for (var index = 0; index < keys.length; index += 1) {
      var value = object[keys[index]];
      if (value !== undefined && value !== null && value !== "") return value;
    }
    return undefined;
  }

  function finiteNumber(value) {
    var number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function timestamp(value) {
    if (value === undefined || value === null || value === "") return null;
    if (typeof value === "number") return Number.isFinite(value) ? value : null;
    if (typeof value === "string") {
      var numeric = Number(value);
      if (Number.isFinite(numeric) && value.trim() !== "") return numeric;
      var parsed = Date.parse(value);
      return Number.isFinite(parsed) ? parsed : null;
    }
    return null;
  }

  function safeText(value, limit) {
    var maxLength = limit || 1600;
    if (value === undefined || value === null) return "";
    var output;
    if (typeof value === "string") {
      output = value;
    } else if (typeof value === "number" || typeof value === "boolean") {
      output = String(value);
    } else {
      try {
        output = JSON.stringify(value, function (_key, nestedValue) {
          if (typeof nestedValue === "bigint") return String(nestedValue);
          return nestedValue;
        });
      } catch (_error) {
        output = String(value);
      }
    }
    if (output.length <= maxLength) return output;
    return output.slice(0, maxLength - 1) + "…";
  }

  function normalizedJoinQuality(value) {
    var quality = String(value || "").toLowerCase();
    return ["structural", "semi", "heuristic", "orphan"].indexOf(quality) >= 0
      ? quality
      : "structural";
  }

  function normalizeKind(raw) {
    var text = String(raw || "").toLowerCase();
    if (text.indexOf("error") >= 0 || text.indexOf("exception") >= 0) return "error";
    if (text.indexOf("agent") >= 0 || text.indexOf("worker") >= 0 || text.indexOf("subagent") >= 0) return "agent";
    if (text.indexOf("tool") >= 0 || text.indexOf("function") >= 0 || text.indexOf("command") >= 0) return "tool";
    if (text.indexOf("llm") >= 0 || text.indexOf("model") >= 0 || text.indexOf("completion") >= 0) return "llm";
    if (text.indexOf("turn") >= 0 || text.indexOf("message") >= 0) return "turn";
    return "other";
  }

  function truthyFlag(raw, name) {
    if (!raw || typeof raw !== "object") return false;
    if (raw[name] === true) return true;
    var joinQuality = normalizedJoinQuality(firstDefined(raw, ["join_quality", "joinQuality"]));
    if (name === "heuristic" && ["heuristic", "orphan"].indexOf(joinQuality) >= 0) return true;
    if (name === "semi" && joinQuality === "semi") return true;
    if (name === "orphan" && joinQuality === "orphan") return true;
    var flags = raw.flags || raw.quality || raw.badges;
    if (Array.isArray(flags)) {
      return flags.some(function (flag) {
        return String(flag).toLowerCase() === name;
      });
    }
    return Boolean(flags && typeof flags === "object" && flags[name]);
  }

  function normalizeAttributes(raw) {
    var source = firstDefined(raw, ["attributes", "details", "metadata", "meta"]);
    if (!source || typeof source !== "object" || Array.isArray(source)) return [];
    return Object.keys(source).slice(0, 32).map(function (key) {
      return { key: safeText(key, 100), value: safeText(source[key], 1800) };
    });
  }

  function warningCount(warnings, keys) {
    var value = finiteNumber(firstDefined(warnings || {}, keys));
    return value === null ? 0 : Math.max(0, Math.round(value));
  }

  function normalizeIdentity(trace) {
    var source = trace && typeof trace.identity === "object" ? trace.identity : {};
    var fields = [
      ["pane target", ["pane_target", "paneTarget"]],
      ["PID", ["pane_pid", "panePid"]],
      ["process start", ["pane_start_time", "paneStartTime"]],
      ["source kind", ["source_kind", "sourceKind"]],
      ["session", ["source_id", "sourceId", "session_signature", "sessionSignature"]],
      ["provider log", ["provider_log", "providerLog"]]
    ];
    var present = [];
    fields.forEach(function (entry) {
      if (firstDefined(source, entry[1]) !== undefined) present.push(entry[0]);
    });
    var explicit = String(firstDefined(source, ["status", "match", "quality"]) || "").toLowerCase();
    var exact = source.exact === true || explicit === "exact";
    return {
      status: exact ? "exact" : (present.length ? "partial" : "unknown"),
      present: present,
      providerLog: safeText(firstDefined(source, ["provider_log", "providerLog"]) || "", 80),
      associationQuality: normalizedJoinQuality(
        firstDefined(source, ["log_association_quality", "logAssociationQuality", "match_quality"])
      ),
      paneInstanceQuality: safeText(
        firstDefined(source, ["pane_instance_quality", "paneInstanceQuality"]) || "",
        40
      ),
      sessionSignature: safeText(
        firstDefined(source, ["session_signature", "sessionSignature"]) || "",
        180
      )
    };
  }

  function isFailed(raw, kind) {
    var status = String(firstDefined(raw, ["status", "state", "outcome"]) || "").toLowerCase();
    return kind === "error" ||
      raw.error === true ||
      Boolean(raw.exception) ||
      ["error", "failed", "failure", "cancelled", "canceled", "timeout"].indexOf(status) >= 0;
  }

  function getRawNodes(trace) {
    if (Array.isArray(trace)) return trace;
    if (!trace || typeof trace !== "object") return [];
    var candidates = ["nodes", "spans", "events", "children"];
    for (var index = 0; index < candidates.length; index += 1) {
      if (Array.isArray(trace[candidates[index]])) return trace[candidates[index]];
    }
    if (trace.root && typeof trace.root === "object") return [trace.root];
    return [];
  }

  function normalizeTrace(input) {
    var trace = input && typeof input === "object" && !Array.isArray(input) ? input : {};
    var rawNodes = getRawNodes(input);
    var nodes = [];
    var byId = Object.create(null);
    var duplicateCounts = Object.create(null);
    var autoId = 0;

    function uniqueId(requested) {
      var base = safeText(requested, 180).trim();
      if (!base) {
        autoId += 1;
        base = "span-" + autoId;
      }
      if (!byId[base]) return base;
      duplicateCounts[base] = (duplicateCounts[base] || 1) + 1;
      return base + "-" + duplicateCounts[base];
    }

    function ingest(rawValue, nestedParentId) {
      var raw = rawValue && typeof rawValue === "object" ? rawValue : { name: rawValue };
      var requestedId = firstDefined(raw, ["id", "span_id", "spanId", "node_id", "nodeId", "event_id"]);
      var id = uniqueId(requestedId);
      var explicitParent = firstDefined(raw, ["parent_id", "parentId", "parent_span_id", "parentSpanId"]);
      var start = timestamp(firstDefined(raw, ["start", "start_ms", "startMs", "started_at", "startedAt", "timestamp", "ts"]));
      var end = timestamp(firstDefined(raw, ["end", "end_ms", "endMs", "ended_at", "endedAt"]));
      var duration = finiteNumber(firstDefined(raw, ["duration_ms", "durationMs", "duration"]));
      var inferredTiming = false;
      if (duration === null && start !== null && end !== null) duration = Math.max(0, end - start);
      if (end === null && start !== null && duration !== null) {
        end = start + Math.max(0, duration);
        inferredTiming = true;
      }
      if (duration !== null) duration = Math.max(0, duration);
      var rawKind = String(firstDefined(raw, ["kind", "type", "category", "span_type", "role"]) || "");
      var rawName = String(firstDefined(raw, ["name", "title", "label", "operation", "event"]) || "");
      var kind = normalizeKind(rawKind);
      if (rawKind.toLowerCase() === "agent_turn") {
        kind = rawName.toLowerCase() === "agent turn" ? "agent" : "turn";
      }
      var failed = rawKind.toLowerCase() === "session" ? false : isFailed(raw, kind);
      var status = safeText(firstDefined(raw, ["status", "state", "outcome"]) || (failed ? "error" : "ok"), 80);
      var badges = QUALITY_BADGES.filter(function (badge) {
        return truthyFlag(raw, badge);
      });
      if (inferredTiming && badges.indexOf("approx") < 0) badges.push("approx");
      var node = {
        id: id,
        sourceId: requestedId === undefined ? "" : safeText(requestedId, 180),
        parentId: explicitParent === undefined || explicitParent === null || explicitParent === ""
          ? (nestedParentId || null)
          : safeText(explicitParent, 180),
        name: safeText(firstDefined(raw, ["name", "title", "label", "operation", "event"]) || KIND_LABELS[kind], 320),
        kind: kind,
        status: status,
        isError: failed,
        start: start,
        end: end,
        duration: duration,
        badges: badges,
        summary: safeText(firstDefined(raw, ["summary", "message", "description", "content"]), 2400),
        input: safeText(firstDefined(raw, ["input_summary", "inputSummary", "input", "arguments", "request"]), 2400),
        output: safeText(firstDefined(raw, ["output_summary", "outputSummary", "output", "result", "response"]), 2400),
        error: safeText(firstDefined(raw, ["error_message", "errorMessage", "exception", "error"]), 2400),
        agentName: safeText(firstDefined(raw, ["agent_name", "agentName", "agent", "worker_name", "workerName"]), 160),
        errorType: safeText(firstDefined(raw, ["error_type", "errorType"]), 80),
        joinQuality: safeText(firstDefined(raw, ["join_quality", "joinQuality"]), 40),
        attributes: normalizeAttributes(raw),
        children: [],
        depth: 0,
        order: nodes.length
      };
      nodes.push(node);
      byId[id] = node;

      var children = Array.isArray(raw.children) ? raw.children : [];
      children.forEach(function (child) {
        ingest(child, id);
      });
    }

    rawNodes.forEach(function (rawNode) {
      ingest(rawNode, null);
    });

    function wouldCycle(node, parent) {
      var seen = Object.create(null);
      seen[node.id] = true;
      var cursor = parent;
      while (cursor) {
        if (seen[cursor.id]) return true;
        seen[cursor.id] = true;
        cursor = cursor.parentId ? byId[cursor.parentId] : null;
      }
      return false;
    }

    var roots = [];
    nodes.forEach(function (node) {
      var parent = node.parentId ? byId[node.parentId] : null;
      if (parent && !wouldCycle(node, parent)) {
        parent.children.push(node.id);
      } else {
        if (node.parentId && node.badges.indexOf("detached") < 0) node.badges.push("detached");
        node.parentId = null;
        roots.push(node.id);
      }
    });

    function assignDepth(id, depth) {
      var node = byId[id];
      if (!node) return;
      node.depth = depth;
      node.children.sort(function (leftId, rightId) {
        var left = byId[leftId];
        var right = byId[rightId];
        var leftTime = left.start === null ? Number.POSITIVE_INFINITY : left.start;
        var rightTime = right.start === null ? Number.POSITIVE_INFINITY : right.start;
        return leftTime - rightTime || left.order - right.order;
      });
      node.children.forEach(function (childId) {
        assignDepth(childId, depth + 1);
      });
    }

    roots.sort(function (leftId, rightId) {
      var left = byId[leftId];
      var right = byId[rightId];
      var leftTime = left.start === null ? Number.POSITIVE_INFINITY : left.start;
      var rightTime = right.start === null ? Number.POSITIVE_INFINITY : right.start;
      return leftTime - rightTime || left.order - right.order;
    });
    roots.forEach(function (rootId) {
      assignDepth(rootId, 0);
    });

    var starts = nodes.map(function (node) { return node.start; }).filter(function (value) { return value !== null; });
    var ends = nodes.map(function (node) { return node.end; }).filter(function (value) { return value !== null; });
    var durations = nodes.map(function (node) { return node.duration; }).filter(function (value) { return value !== null; });
    var minStart = starts.length ? Math.min.apply(Math, starts) : 0;
    var maxEnd = ends.length ? Math.max.apply(Math, ends) : minStart;
    var maxDuration = durations.length ? Math.max.apply(Math, durations) : 0;
    var traceDuration = finiteNumber(firstDefined(trace, ["duration_ms", "durationMs", "duration"]));
    if (traceDuration === null && maxEnd > minStart) traceDuration = maxEnd - minStart;

    var model = {
      id: safeText(firstDefined(trace, ["trace_id", "traceId", "session_id", "sessionId", "id"]) || "", 180),
      title: safeText(
        firstDefined(trace, ["title", "name", "label"]) ||
        (trace.source ? String(trace.source) + " session" : "Execution trace"),
        320
      ),
      status: safeText(
        firstDefined(trace, ["status", "state", "outcome"]) ||
        (trace.summary && trace.summary.status) || "",
        80
      ),
      duration: traceDuration,
      nodes: nodes,
      byId: byId,
      roots: roots,
      timeline: {
        minStart: minStart,
        maxEnd: maxEnd,
        maxDuration: maxDuration
      },
      identity: normalizeIdentity(trace),
      quality: normalizeQuality(trace),
      usage: normalizeUsage(trace),
      warnings: normalizeWarnings(trace.warnings),
      truncated: trace.truncated === true,
      source: safeText(trace.source || "", 80)
    };
    model.sessionKey = model.identity.sessionSignature || model.id;
    model.summary = computeSummary(model);
    if (trace.summary && typeof trace.summary === "object") {
      var provided = trace.summary;
      [
        ["turn", "turn_count"],
        ["llm", "llm_count"],
        ["tool", "tool_count"],
        ["error", "error_count"]
      ].forEach(function (mapping) {
        var value = finiteNumber(provided[mapping[1]]);
        if (value !== null) model.summary[mapping[0]] = Math.max(0, Math.round(value));
      });
      if (model.duration === null) {
        model.duration = finiteNumber(provided.duration_ms);
      }
    }
    return model;
  }

  function computeSummary(model) {
    var counts = { turn: 0, llm: 0, tool: 0, agent: 0, error: 0 };
    (model.nodes || []).forEach(function (node) {
      if (Object.prototype.hasOwnProperty.call(counts, node.kind) && node.kind !== "error") {
        counts[node.kind] += 1;
      }
      if (node.isError) counts.error += 1;
    });
    return counts;
  }

  function normalizeQuality(trace) {
    var quality = trace && typeof trace.quality === "object" ? trace.quality : {};
    var joins = quality.join_quality && typeof quality.join_quality === "object"
      ? quality.join_quality
      : quality.joins && typeof quality.joins === "object" ? quality.joins : {};
    var counts = {};
    ["structural", "semi", "heuristic", "orphan"].forEach(function (name) {
      counts[name] = warningCount(joins, [name]);
    });
    var signals = [];
    if (Array.isArray(quality.signals)) {
      signals = quality.signals.slice(0, 24).map(function (value) { return safeText(value, 180); });
    } else if (quality.signals && typeof quality.signals === "object") {
      Object.keys(quality.signals).slice(0, 24).forEach(function (key) {
        var count = warningCount(quality.signals, [key]);
        if (count) signals.push(safeText(key, 80) + ": " + count);
      });
    }
    var reasons = [];
    if (Array.isArray(quality.reasons)) {
      reasons = quality.reasons.slice(0, 24).map(function (value) { return safeText(value, 500); });
    } else if (quality.reasons && typeof quality.reasons === "object") {
      Object.keys(quality.reasons).slice(0, 24).forEach(function (key) {
        var active = key === "structural" || counts[key] > 0 ||
          signals.some(function (signal) { return signal.indexOf(key + ":") === 0; });
        if (active) reasons.push(safeText(key + "：" + quality.reasons[key], 500));
      });
    }
    return {
      joins: counts,
      signals: signals,
      reasons: reasons,
      completeness: safeText(firstDefined(quality, ["completeness", "status"]) || "", 80),
      timeQuality: safeText(firstDefined(quality, ["time_quality", "timeQuality"]) || "", 80)
    };
  }

  function normalizeWarnings(value) {
    if (Array.isArray(value)) {
      return value.slice(0, 32).map(function (warning) { return safeText(warning, 500); });
    }
    if (!value || typeof value !== "object") return [];
    return Object.keys(value).slice(0, 32).reduce(function (items, key) {
      var count = warningCount(value, [key]);
      if (count) items.push(safeText(key, 80) + ": " + count);
      return items;
    }, []);
  }

  function normalizeUsage(trace) {
    var usage = trace && typeof trace.usage === "object" ? trace.usage : {};
    var tokens = usage.tokens && typeof usage.tokens === "object" ? usage.tokens : {};
    var cost = usage.cost && typeof usage.cost === "object" ? usage.cost : {};
    return {
      tokens: {
        quality: safeText(tokens.quality || "unknown", 40),
        input: finiteNumber(firstDefined(tokens, ["input", "input_tokens", "inputTokens"])),
        output: finiteNumber(firstDefined(tokens, ["output", "output_tokens", "outputTokens"])),
        total: finiteNumber(firstDefined(tokens, ["total", "total_tokens", "totalTokens"]))
      },
      cost: {
        quality: safeText(cost.quality || "unknown", 40),
        reason: safeText(cost.reason || "日志没有可核验账单边界", 220)
      }
    };
  }

  function matchesFilter(node, filter) {
    if (filter === "all") return true;
    if (filter === "error") return node.isError;
    if (filter === "output") return node.kind === "llm" || Boolean(node.output);
    if (filter === "agent") return node.kind === "agent" || Boolean(node.agentName);
    return node.kind === filter;
  }

  function filterVisibleIds(model, filter) {
    var requested = FILTERS.indexOf(filter) >= 0 ? filter : "all";
    var visible = Object.create(null);
    if (requested === "all") {
      model.nodes.forEach(function (node) { visible[node.id] = true; });
      return visible;
    }
    model.nodes.forEach(function (node) {
      var matches = matchesFilter(node, requested);
      if (!matches) return;
      var cursor = node;
      while (cursor && !visible[cursor.id]) {
        visible[cursor.id] = true;
        cursor = cursor.parentId ? model.byId[cursor.parentId] : null;
      }
    });
    return visible;
  }

  function flattenVisible(model, filter, collapsed) {
    var visible = filterVisibleIds(model, filter);
    var output = [];
    function visit(id) {
      var node = model.byId[id];
      if (!node || !visible[id]) return;
      output.push({
        node: node,
        context: filter !== "all" && !matchesFilter(node, filter)
      });
      if (collapsed && collapsed.has(id)) return;
      node.children.forEach(visit);
    }
    model.roots.forEach(visit);
    return output;
  }

  function barGeometry(node, timeline) {
    var minStart = timeline.minStart;
    var range = Math.max(0, timeline.maxEnd - minStart);
    var left = 0;
    var width = 3;
    if (range > 0 && node.start !== null) {
      left = Math.max(0, Math.min(100, ((node.start - minStart) / range) * 100));
      var nodeEnd = node.end !== null
        ? node.end
        : node.start + (node.duration === null ? 0 : node.duration);
      width = Math.max(0.8, ((Math.max(node.start, nodeEnd) - node.start) / range) * 100);
    } else if (node.duration !== null && timeline.maxDuration > 0) {
      width = Math.max(0.8, (node.duration / timeline.maxDuration) * 100);
    }
    width = Math.max(0.8, Math.min(100 - left, width));
    return {
      left: Math.round(left * 1000) / 1000,
      width: Math.round(width * 1000) / 1000
    };
  }

  function searchTrace(model, query) {
    var needle = safeText(query, 240).trim().toLocaleLowerCase();
    if (!needle) return [];
    var matches = [];
    model.nodes.some(function (node) {
      var fields = {
        name: node.name,
        input: node.input,
        output: node.output,
        tool: node.kind === "tool" ? node.name + " " + node.input + " " + node.output : "",
        error: node.error + " " + node.errorType,
        agent: node.agentName + (node.kind === "agent" ? " " + node.name : "")
      };
      var hitFields = SEARCH_FIELDS.filter(function (field) {
        return String(fields[field] || "").toLocaleLowerCase().indexOf(needle) >= 0;
      });
      if (hitFields.length) matches.push({ id: node.id, fields: hitFields });
      return matches.length >= MAX_SEARCH_MATCHES;
    });
    return matches;
  }

  function pathIds(model, ids) {
    var visible = Object.create(null);
    ids.forEach(function (id) {
      var cursor = model.byId[id];
      while (cursor && !visible[cursor.id]) {
        visible[cursor.id] = true;
        cursor = cursor.parentId ? model.byId[cursor.parentId] : null;
      }
    });
    return visible;
  }

  function buildSwimlanes(model, visibleIds) {
    var lanes = Object.create(null);
    var totalBlocks = 0;
    function inheritedAgent(node) {
      var cursor = node;
      while (cursor) {
        if (cursor.agentName) return cursor.agentName;
        if (cursor.kind === "agent") return cursor.name;
        cursor = cursor.parentId ? model.byId[cursor.parentId] : null;
      }
      return "主智能体";
    }
    model.nodes.forEach(function (node) {
      if (visibleIds && !visibleIds[node.id]) return;
      var agent = inheritedAgent(node);
      if (!lanes[agent]) lanes[agent] = { name: agent, blocks: [] };
      if (totalBlocks < MAX_SWIMLANE_BLOCKS) {
        lanes[agent].blocks.push(node);
        totalBlocks += 1;
      }
    });
    return Object.keys(lanes).sort(function (left, right) {
      if (left === "主智能体") return -1;
      if (right === "主智能体") return 1;
      return left.localeCompare(right);
    }).map(function (name) {
      lanes[name].blocks.sort(function (left, right) {
        var leftStart = left.start === null ? Number.POSITIVE_INFINITY : left.start;
        var rightStart = right.start === null ? Number.POSITIVE_INFINITY : right.start;
        return leftStart - rightStart || left.order - right.order;
      });
      return lanes[name];
    });
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function looksSensitive(value) {
    var text = String(value || "");
    var candidates = text.match(/[A-Za-z0-9_+/=-]{32,}/g) || [];
    return /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/i.test(text) ||
      /\b(?:password|api[_-]?key|secret)\s*[:=]\s*(?!\[REDACTED)[^\s"',;}\]]{8,}/i.test(text) ||
      /\bBearer\s+[A-Za-z0-9._~+\/=-]{12,}/i.test(text) ||
      /\bBasic\s+[A-Za-z0-9+/=]{12,}/i.test(text) ||
      /\b[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}\b/.test(text) ||
      /\b(?:sk|ghp|github_pat|npm)_[A-Za-z0-9_-]{12,}/i.test(text) ||
      /\bxox[baprs]-[A-Za-z0-9-]{10,}/i.test(text) ||
      candidates.some(looksHighEntropy);
  }

  function looksHighEntropy(candidate) {
    if (!candidate || candidate.length < 32 || /^[0-9a-f-]+$/i.test(candidate)) return false;
    var counts = Object.create(null);
    Array.from(candidate).forEach(function (character) {
      counts[character] = (counts[character] || 0) + 1;
    });
    var length = candidate.length;
    var entropy = Object.keys(counts).reduce(function (total, key) {
      var probability = counts[key] / length;
      return total - probability * (Math.log(probability) / Math.log(2));
    }, 0);
    return entropy >= 4.25;
  }

  function exportTrace(model, format) {
    var metadata = {
      type: "trace",
      schema_version: 1,
      id: model.id,
      title: model.title,
      status: model.status,
      summary: model.summary,
      identity: model.identity,
      quality: model.quality,
      usage: model.usage,
      truncated: model.truncated,
      warnings: model.warnings
    };
    var spans = model.nodes.map(function (node) {
      return {
        type: "span",
        id: node.id,
        parent_id: node.parentId,
        name: node.name,
        kind: node.kind,
        status: node.status,
        agent_name: node.agentName,
        error_type: node.errorType,
        error: node.error,
        summary: node.summary,
        input: node.input,
        output: node.output,
        start_ms: node.start,
        end_ms: node.end,
        duration_ms: node.duration,
        join_quality: node.joinQuality,
        badges: node.badges
      };
    });
    var serialized = JSON.stringify([metadata].concat(spans));
    if (looksSensitive(serialized)) {
      throw new Error("导出已拒绝：脱敏后的轨迹仍疑似包含凭证。正常查看不受影响。");
    }
    if (format === "ndjson") {
      return {
        content: [metadata].concat(spans).map(function (item) { return JSON.stringify(item); }).join("\n") + "\n",
        mime: "application/x-ndjson;charset=utf-8",
        extension: "ndjson",
        filename: "trace-" + (model.id || "session") + ".ndjson"
      };
    }
    if (format !== "html") throw new Error("Unsupported export format.");
    var rows = spans.map(function (span) {
      return "<tr><td>" + escapeHtml(span.agent_name || "主智能体") +
        "</td><td>" + escapeHtml(span.kind) +
        "</td><td>" + escapeHtml(span.name) +
        "</td><td>" + escapeHtml(span.status) +
        "</td><td><pre>" + escapeHtml(span.error || span.output || span.summary) +
        "</pre></td></tr>";
    }).join("");
    var html = "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">" +
      "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">" +
      "<title>" + escapeHtml(model.title) + "</title><style>" +
      "body{font:14px/1.5 system-ui;margin:24px;color:#182229;background:#f5f7f3}" +
      "h1{font-size:24px}table{width:100%;border-collapse:collapse;background:white}" +
      "th,td{padding:9px;border:1px solid #d8dfd9;text-align:left;vertical-align:top}" +
      "pre{white-space:pre-wrap;word-break:break-word;margin:0}</style><body><h1>" +
      escapeHtml(model.title) + "</h1><p>状态：" + escapeHtml(model.status || "未知") +
      "；节点：" + spans.length + "；错误：" + model.summary.error +
      "</p><table><thead><tr><th>智能体</th><th>类型</th><th>节点</th><th>状态</th><th>安全摘要</th></tr></thead><tbody>" +
      rows + "</tbody></table></body></html>";
    return {
      content: html,
      mime: "text/html;charset=utf-8",
      extension: "html",
      filename: "trace-" + (model.id || "session") + ".html"
    };
  }

  function formatDuration(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
    var milliseconds = Math.max(0, Number(value));
    if (milliseconds < 1) return milliseconds.toFixed(2) + " ms";
    if (milliseconds < 1000) return Math.round(milliseconds) + " ms";
    if (milliseconds < 60000) return (milliseconds / 1000).toFixed(milliseconds < 10000 ? 2 : 1) + " s";
    var minutes = Math.floor(milliseconds / 60000);
    var seconds = Math.round((milliseconds % 60000) / 1000);
    if (minutes < 60) return minutes + "m " + seconds + "s";
    var hours = Math.floor(minutes / 60);
    var remainingMinutes = minutes % 60;
    if (hours < 24) return hours + "h " + remainingMinutes + "m";
    var days = Math.floor(hours / 24);
    return days + "d " + (hours % 24) + "h";
  }

  function formatTimestamp(value) {
    if (value === null || value === undefined) return "—";
    if (Math.abs(value) < 100000000000) return safeText(value, 80);
    try {
      return new Date(value).toLocaleString();
    } catch (_error) {
      return safeText(value, 80);
    }
  }

  function createElement(tag, className, textValue) {
    var element = document.createElement(tag);
    if (className) element.className = className;
    if (textValue !== undefined && textValue !== null) element.textContent = String(textValue);
    return element;
  }

  function appendHighlighted(parent, value, query) {
    var textValue = String(value || "");
    var needle = String(query || "").trim();
    if (!needle) {
      parent.appendChild(document.createTextNode(textValue));
      return;
    }
    var lower = textValue.toLocaleLowerCase();
    var lowerNeedle = needle.toLocaleLowerCase();
    var cursor = 0;
    while (cursor < textValue.length) {
      var found = lower.indexOf(lowerNeedle, cursor);
      if (found < 0) {
        parent.appendChild(document.createTextNode(textValue.slice(cursor)));
        break;
      }
      if (found > cursor) parent.appendChild(document.createTextNode(textValue.slice(cursor, found)));
      parent.appendChild(createElement("mark", "ctv-highlight", textValue.slice(found, found + needle.length)));
      cursor = found + needle.length;
    }
  }

  function appendTextBlock(parent, label, value, tone) {
    if (!value) return;
    var block = createElement("section", "ctv-detail-block" + (tone ? " ctv-detail-block--" + tone : ""));
    block.appendChild(createElement("h4", "ctv-detail-label", label));
    block.appendChild(createElement("pre", "ctv-detail-value", value));
    parent.appendChild(block);
  }

  function create(root) {
    if (!root || typeof root.appendChild !== "function") {
      throw new TypeError("CardsTraceView.create(root) requires a DOM element.");
    }

    var mount = createElement("section", "ctv");
    mount.setAttribute("aria-label", "Execution trace");
    root.appendChild(mount);

    var state = {
      model: null,
      rawTrace: null,
      filter: "all",
      view: "tree",
      collapsed: new Set(),
      selectedId: null,
      query: "",
      searchMatches: [],
      matchIndex: -1,
      errorPathOnly: false,
      searchTimer: null,
      destroyed: false
    };

    function wipe() {
      while (mount.firstChild) mount.removeChild(mount.firstChild);
      mount.classList.remove("ctv-mobile-detail-open");
    }

    function renderState(kind, message) {
      if (state.destroyed) return;
      wipe();
      var panel = createElement("div", "ctv-state ctv-state--" + kind);
      if (kind === "loading") {
        var spinner = createElement("span", "ctv-spinner");
        spinner.setAttribute("aria-hidden", "true");
        panel.appendChild(spinner);
      }
      panel.appendChild(createElement("strong", "ctv-state-title", kind === "error" ? "Trace unavailable" : kind === "empty" ? "No trace data" : "Loading trace"));
      panel.appendChild(createElement("span", "ctv-state-copy", message || (kind === "loading" ? "Collecting spans…" : "")));
      if (kind === "loading") {
        panel.setAttribute("role", "status");
        panel.setAttribute("aria-live", "polite");
      } else if (kind === "error") {
        panel.setAttribute("role", "alert");
      }
      mount.appendChild(panel);
    }

    function detailLine(list, label, value) {
      var row = createElement("div", "ctv-fact");
      row.appendChild(createElement("dt", "ctv-fact-key", label));
      row.appendChild(createElement("dd", "ctv-fact-value", value || "—"));
      list.appendChild(row);
    }

    function renderDetail(node) {
      var panel = mount.querySelector(".ctv-detail");
      if (!panel) return;
      while (panel.firstChild) panel.removeChild(panel.firstChild);

      var toolbar = createElement("div", "ctv-detail-toolbar");
      var back = createElement("button", "ctv-button ctv-detail-back", "← Back to trace");
      back.type = "button";
      back.setAttribute("data-ctv-action", "detail-back");
      toolbar.appendChild(back);
      toolbar.appendChild(createElement("span", "ctv-detail-eyebrow", node ? "SPAN DETAILS" : "INSPECTOR"));
      panel.appendChild(toolbar);

      if (!node) {
        var empty = createElement("div", "ctv-detail-empty");
        empty.appendChild(createElement("strong", "", "Select a trace span"));
        empty.appendChild(createElement("span", "", "Its timing, status, inputs and outputs will appear here."));
        panel.appendChild(empty);
        return;
      }

      var heading = createElement("div", "ctv-detail-heading");
      heading.appendChild(createElement("span", "ctv-kind ctv-kind--" + node.kind, KIND_LABELS[node.kind]));
      heading.appendChild(createElement("h3", "ctv-detail-title", node.name));
      panel.appendChild(heading);

      if (node.badges.length) {
        var badges = createElement("div", "ctv-badges ctv-badges--detail");
        node.badges.forEach(function (badge) {
          badges.appendChild(createElement("span", "ctv-badge ctv-badge--" + badge, badge));
        });
        panel.appendChild(badges);
      }

      var facts = createElement("dl", "ctv-facts");
      detailLine(facts, "Status", node.status);
      detailLine(facts, "Duration", formatDuration(node.duration));
      detailLine(facts, "Started", formatTimestamp(node.start));
      detailLine(facts, "Ended", formatTimestamp(node.end));
      detailLine(facts, "Span ID", node.sourceId || node.id);
      if (node.parentId) detailLine(facts, "Parent", node.parentId);
      if (node.agentName) detailLine(facts, "智能体", node.agentName);
      if (node.errorType) detailLine(facts, "错误类型", node.errorType);
      if (node.joinQuality) detailLine(facts, "Join quality", node.joinQuality);
      if (node.isError && state.model) {
        var previous = null;
        state.model.nodes.forEach(function (candidate) {
          if (candidate.id === node.id || candidate.start === null || node.start === null || candidate.start > node.start) return;
          if (candidate.kind !== "tool" && candidate.kind !== "llm" && candidate.kind !== "agent") return;
          if (!previous || candidate.start > previous.start) previous = candidate;
        });
        if (previous) detailLine(facts, "前一个关键操作", previous.name);
      }
      panel.appendChild(facts);

      appendTextBlock(panel, "Summary", node.summary);
      appendTextBlock(panel, "Input", node.input);
      appendTextBlock(panel, "Output", node.output);
      appendTextBlock(panel, "Error", node.error, "error");

      if (node.attributes.length) {
        var attributes = createElement("section", "ctv-detail-block");
        attributes.appendChild(createElement("h4", "ctv-detail-label", "Attributes"));
        var attributeList = createElement("dl", "ctv-attributes");
        node.attributes.forEach(function (attribute) {
          detailLine(attributeList, attribute.key, attribute.value);
        });
        attributes.appendChild(attributeList);
        panel.appendChild(attributes);
      }
    }

    function summaryItem(kind, label, count) {
      var item = createElement("div", "ctv-summary-item ctv-summary-item--" + kind);
      item.appendChild(createElement("span", "ctv-summary-count", count));
      item.appendChild(createElement("span", "ctv-summary-label", label));
      return item;
    }

    function renderQualityPanel() {
      var panel = createElement("section", "ctv-quality");
      var heading = createElement("div", "ctv-quality-heading");
      heading.appendChild(createElement("strong", "", "轨迹可信度"));
      heading.appendChild(createElement(
        "span",
        "ctv-quality-status ctv-quality-status--" + state.model.identity.status,
        state.model.identity.status === "exact" ? "身份精确对应" :
          state.model.identity.status === "partial" ? "身份部分确认" : "身份不确定"
      ));
      panel.appendChild(heading);
      var grid = createElement("dl", "ctv-quality-grid");
      detailLine(grid, "身份依据", state.model.identity.present.length
        ? state.model.identity.present.join("、")
        : "日志未提供可核验身份依据");
      detailLine(
        grid,
        "Card→日志",
        state.model.identity.associationQuality === "structural" ? "结构化对应" :
          state.model.identity.associationQuality === "semi" ? "强结构信息推导" :
            state.model.identity.associationQuality === "heuristic" ? "文本或时间推测" :
              "无法确认"
      );
      var joins = state.model.quality.joins;
      detailLine(
        grid,
        "父子关系",
        "结构 " + joins.structural + " · 半结构 " + joins.semi +
          " · 推测 " + joins.heuristic + " · 未确认 " + joins.orphan
      );
      detailLine(grid, "完整性", state.model.truncated
        ? "truncated（因安全上限截断）"
        : (state.model.quality.completeness || state.model.status || "未知"));
      detailLine(grid, "时间质量", state.model.quality.timeQuality || "日志未明确说明");
      var tokens = state.model.usage.tokens;
      detailLine(grid, "Token", tokens.quality === "exact"
        ? "精确记录 " + (tokens.total === null ? "（总数未知）" : tokens.total)
        : "未知（不做伪精确估算）");
      detailLine(grid, "费用", "未知（" + state.model.usage.cost.reason + "）");
      panel.appendChild(grid);
      var explanations = state.model.quality.reasons.concat(state.model.warnings);
      if (explanations.length) {
        var list = createElement("ul", "ctv-quality-reasons");
        explanations.slice(0, 12).forEach(function (reason) {
          list.appendChild(createElement("li", "", reason));
        });
        panel.appendChild(list);
      }
      panel.appendChild(createElement(
        "p",
        "ctv-quality-note",
        "这些标记只解释数据可信度，不会阻止或修改智能体运行。"
      ));
      return panel;
    }

    function rowFor(entry) {
      var node = entry.node;
      var row = createElement("div", "ctv-node ctv-node--" + node.kind + (entry.context ? " ctv-node--context" : ""));
      row.setAttribute("role", "treeitem");
      row.setAttribute("aria-level", node.depth + 1);
      row.setAttribute("aria-selected", node.id === state.selectedId ? "true" : "false");
      row.setAttribute("data-ctv-node-id", node.id);
      row.tabIndex = node.id === state.selectedId ? 0 : -1;
      row.style.setProperty("--ctv-depth", String(node.depth));

      var hasChildren = node.children.length > 0;
      var toggle = createElement("button", "ctv-disclosure", hasChildren ? (state.collapsed.has(node.id) ? "›" : "⌄") : "");
      toggle.type = "button";
      toggle.tabIndex = -1;
      toggle.disabled = !hasChildren;
      toggle.setAttribute("data-ctv-action", "toggle-node");
      toggle.setAttribute("aria-label", hasChildren ? (state.collapsed.has(node.id) ? "Expand span" : "Collapse span") : "No child spans");
      if (hasChildren) toggle.setAttribute("aria-expanded", state.collapsed.has(node.id) ? "false" : "true");
      row.appendChild(toggle);

      var main = createElement("div", "ctv-node-main");
      var titleLine = createElement("div", "ctv-node-titleline");
      titleLine.appendChild(createElement("span", "ctv-kind ctv-kind--" + node.kind, KIND_LABELS[node.kind]));
      var nodeName = createElement("span", "ctv-node-name");
      appendHighlighted(nodeName, node.name, state.query);
      titleLine.appendChild(nodeName);
      if (node.agentName) titleLine.appendChild(createElement("span", "ctv-node-agent", node.agentName));
      if (node.badges.length) {
        var badges = createElement("span", "ctv-badges");
        node.badges.forEach(function (badge) {
          badges.appendChild(createElement("span", "ctv-badge ctv-badge--" + badge, badge));
        });
        titleLine.appendChild(badges);
      }
      main.appendChild(titleLine);

      var lane = createElement("span", "ctv-duration-lane");
      var bar = createElement("span", "ctv-duration-bar");
      var geometry = barGeometry(node, state.model.timeline);
      bar.style.setProperty("--ctv-bar-left", geometry.left + "%");
      bar.style.setProperty("--ctv-bar-width", geometry.width + "%");
      lane.appendChild(bar);
      main.appendChild(lane);
      row.appendChild(main);

      var duration = createElement("span", "ctv-node-duration", formatDuration(node.duration));
      duration.title = node.duration === null ? "Duration unavailable" : String(node.duration) + " ms";
      row.appendChild(duration);
      return row;
    }

    function refreshRows(options) {
      var tree = mount.querySelector(".ctv-tree");
      if (!tree || !state.model) return;
      var treePanel = mount.querySelector(".ctv-tree-panel");
      var swimlanePanel = mount.querySelector(".ctv-swimlanes");
      var focusId = options && options.focusId;
      var scrollTop = tree.scrollTop;
      while (tree.firstChild) tree.removeChild(tree.firstChild);
      var visible = filterVisibleIds(state.model, state.filter);
      var directMatches = Object.create(null);
      state.searchMatches.forEach(function (match) { directMatches[match.id] = true; });
      if (state.query) {
        var searchPaths = pathIds(state.model, state.searchMatches.map(function (match) { return match.id; }));
        Object.keys(visible).forEach(function (id) {
          if (!searchPaths[id]) delete visible[id];
        });
      }
      if (state.errorPathOnly) {
        var errorPaths = pathIds(state.model, state.model.nodes.filter(function (node) {
          return node.isError;
        }).map(function (node) { return node.id; }));
        Object.keys(visible).forEach(function (id) {
          if (!errorPaths[id]) delete visible[id];
        });
      }
      var entries = [];
      function visit(id) {
        var node = state.model.byId[id];
        if (!node || !visible[id]) return;
        entries.push({
          node: node,
          context: !matchesFilter(node, state.filter) || (state.query && !directMatches[id])
        });
        if (state.collapsed.has(id)) return;
        node.children.forEach(visit);
      }
      state.model.roots.forEach(visit);
      if (!entries.length) {
        var noMatches = createElement("div", "ctv-no-matches");
        noMatches.appendChild(createElement("strong", "", "No matching spans"));
        noMatches.appendChild(createElement("span", "", "请调整搜索词或筛选条件。"));
        tree.appendChild(noMatches);
        renderDetail(null);
        return;
      }
      if (!entries.some(function (entry) { return entry.node.id === state.selectedId; })) {
        state.selectedId = entries[0].node.id;
      }
      entries.slice(0, MAX_RENDERED_ROWS).forEach(function (entry) { tree.appendChild(rowFor(entry)); });
      if (entries.length > MAX_RENDERED_ROWS) {
        tree.appendChild(createElement(
          "div",
          "ctv-render-limit",
          "为保持页面流畅，仅显示前 " + MAX_RENDERED_ROWS + " 个可见节点；请使用搜索或筛选缩小范围。"
        ));
      }
      renderDetail(state.model.byId[state.selectedId]);
      tree.scrollTop = scrollTop;
      if (treePanel) treePanel.hidden = state.view !== "tree";
      if (swimlanePanel) {
        swimlanePanel.hidden = state.view !== "swimlane";
        if (state.view === "swimlane") renderSwimlanes(visible);
      }
      if (focusId) {
        var focusTarget = Array.prototype.find.call(tree.querySelectorAll(".ctv-node"), function (candidate) {
          return candidate.getAttribute("data-ctv-node-id") === focusId;
        });
        if (focusTarget) focusTarget.focus();
      }
    }

    function renderSwimlanes(visible) {
      var panel = mount.querySelector(".ctv-swimlanes");
      if (!panel || !state.model) return;
      var previousScroll = panel.scrollLeft;
      while (panel.firstChild) panel.removeChild(panel.firstChild);
      var head = createElement("div", "ctv-swimlane-head");
      head.appendChild(createElement("strong", "", "多智能体时间泳道"));
      head.appendChild(createElement("span", "", "横轴为时间；虚线块表示推断关系"));
      panel.appendChild(head);
      var lanes = buildSwimlanes(state.model, visible);
      lanes.forEach(function (lane) {
        var row = createElement("div", "ctv-swimlane");
        row.appendChild(createElement("div", "ctv-swimlane-label", lane.name));
        var track = createElement("div", "ctv-swimlane-track");
        lane.blocks.forEach(function (node) {
          var block = createElement("button", "ctv-swimlane-block ctv-swimlane-block--" + node.kind);
          if (node.badges.indexOf("heuristic") >= 0 || node.badges.indexOf("orphan") >= 0) {
            block.classList.add("ctv-swimlane-block--heuristic");
          }
          if (node.isError) block.classList.add("ctv-swimlane-block--error");
          var geometry = barGeometry(node, state.model.timeline);
          block.style.setProperty("--ctv-block-left", geometry.left + "%");
          block.style.setProperty("--ctv-block-width", Math.max(1.2, geometry.width) + "%");
          block.type = "button";
          block.title = node.name + " · " + formatDuration(node.duration);
          block.setAttribute("data-ctv-node-id", node.id);
          block.setAttribute("data-ctv-action", "select-swimlane-node");
          block.setAttribute("aria-label", lane.name + "，" + node.name);
          track.appendChild(block);
        });
        row.appendChild(track);
        panel.appendChild(row);
      });
      panel.scrollLeft = previousScroll;
    }

    function render(trace, preserveState) {
      if (state.destroyed) return;
      var previousSessionKey = state.model && state.model.sessionKey;
      var previous = {
        filter: state.filter,
        view: state.view,
        collapsed: new Set(state.collapsed),
        selectedId: state.selectedId,
        query: state.query,
        matchIndex: state.matchIndex,
        errorPathOnly: state.errorPathOnly
      };
      state.rawTrace = trace;
      state.model = normalizeTrace(trace);
      var sameSession = preserveState === true &&
        previousSessionKey &&
        previousSessionKey === state.model.sessionKey;
      if (sameSession) {
        state.filter = previous.filter;
        state.view = previous.view;
        state.collapsed = previous.collapsed;
        state.selectedId = state.model.byId[previous.selectedId] ? previous.selectedId : null;
        state.query = previous.query;
        state.matchIndex = previous.matchIndex;
        state.errorPathOnly = previous.errorPathOnly;
      } else {
        state.filter = "all";
        state.view = "tree";
        state.query = "";
        state.matchIndex = -1;
        state.errorPathOnly = false;
        state.collapsed = new Set();
        state.model.nodes.forEach(function (node) {
          if (node.depth >= 1 && node.children.length) state.collapsed.add(node.id);
        });
        state.selectedId = state.model.roots[0] || (state.model.nodes[0] && state.model.nodes[0].id) || null;
      }
      state.searchMatches = searchTrace(state.model, state.query);
      if (!state.model.nodes.length) {
        renderState("empty", "This trace does not contain any spans.");
        return;
      }

      var mountScroll = mount.scrollTop;
      wipe();
      var header = createElement("header", "ctv-header");
      var identity = createElement("div", "ctv-identity");
      identity.appendChild(createElement("span", "ctv-kicker", "EXECUTION TRACE"));
      identity.appendChild(createElement("h2", "ctv-title", state.model.title));
      var meta = createElement("div", "ctv-meta");
      if (state.model.id) meta.appendChild(createElement("span", "ctv-meta-id", state.model.id));
      if (state.model.status) meta.appendChild(createElement("span", "ctv-status", state.model.status));
      meta.appendChild(createElement("span", "ctv-meta-duration", formatDuration(state.model.duration)));
      identity.appendChild(meta);
      header.appendChild(identity);

      var summary = createElement("div", "ctv-summary");
      summary.setAttribute("aria-label", "Trace summary");
      summary.appendChild(summaryItem("turn", "turn", state.model.summary.turn));
      summary.appendChild(summaryItem("llm", "LLM", state.model.summary.llm));
      summary.appendChild(summaryItem("tool", "tool", state.model.summary.tool));
      summary.appendChild(summaryItem("agent", "agent", state.model.summary.agent));
      summary.appendChild(summaryItem("error", "error", state.model.summary.error));
      header.appendChild(summary);
      mount.appendChild(header);
      mount.appendChild(renderQualityPanel());

      var controls = createElement("div", "ctv-controls");
      var search = createElement("label", "ctv-search");
      search.appendChild(createElement("span", "ctv-search-label", "搜索轨迹"));
      var searchInput = createElement("input", "ctv-search-input");
      searchInput.type = "search";
      searchInput.value = state.query;
      searchInput.placeholder = "输入、输出、工具、错误或智能体";
      searchInput.setAttribute("data-ctv-search", "true");
      searchInput.autocomplete = "off";
      search.appendChild(searchInput);
      controls.appendChild(search);
      var searchNav = createElement("div", "ctv-search-nav");
      var previousMatch = createElement("button", "ctv-button", "上一个");
      previousMatch.type = "button";
      previousMatch.setAttribute("data-ctv-action", "previous-match");
      var nextMatch = createElement("button", "ctv-button", "下一个");
      nextMatch.type = "button";
      nextMatch.setAttribute("data-ctv-action", "next-match");
      searchNav.appendChild(previousMatch);
      searchNav.appendChild(nextMatch);
      searchNav.appendChild(createElement(
        "span",
        "ctv-search-count",
        state.query ? state.searchMatches.length + " 个结果" : "未搜索"
      ));
      controls.appendChild(searchNav);
      mount.appendChild(controls);

      var filterbar = createElement("div", "ctv-filterbar");
      filterbar.setAttribute("role", "toolbar");
      filterbar.setAttribute("aria-label", "Filter trace spans");
      [
        { id: "all", label: "全部", count: state.model.nodes.length },
        { id: "error", label: "错误", count: state.model.summary.error },
        { id: "tool", label: "工具", count: state.model.summary.tool },
        { id: "output", label: "模型输出", count: state.model.summary.llm },
        { id: "agent", label: "子智能体", count: state.model.summary.agent }
      ].forEach(function (filter) {
        var button = createElement("button", "ctv-filter" + (filter.id === state.filter ? " is-active" : ""));
        button.type = "button";
        button.setAttribute("data-ctv-filter", filter.id);
        button.setAttribute("aria-pressed", filter.id === state.filter ? "true" : "false");
        button.appendChild(createElement("span", "", filter.label));
        button.appendChild(createElement("span", "ctv-filter-count", filter.count));
        filterbar.appendChild(button);
      });
      var pathButton = createElement("button", "ctv-filter" + (state.errorPathOnly ? " is-active" : ""), "只看错误路径");
      pathButton.type = "button";
      pathButton.setAttribute("data-ctv-action", "toggle-error-path");
      pathButton.setAttribute("aria-pressed", state.errorPathOnly ? "true" : "false");
      filterbar.appendChild(pathButton);
      var prevError = createElement("button", "ctv-button", "← 上个错误");
      prevError.type = "button";
      prevError.setAttribute("data-ctv-action", "previous-error");
      var nextError = createElement("button", "ctv-button", "下个错误 →");
      nextError.type = "button";
      nextError.setAttribute("data-ctv-action", "next-error");
      filterbar.appendChild(prevError);
      filterbar.appendChild(nextError);
      var treeView = createElement("button", "ctv-view-toggle" + (state.view === "tree" ? " is-active" : ""), "轨迹树");
      treeView.type = "button";
      treeView.setAttribute("data-ctv-view", "tree");
      var swimView = createElement("button", "ctv-view-toggle" + (state.view === "swimlane" ? " is-active" : ""), "泳道图");
      swimView.type = "button";
      swimView.setAttribute("data-ctv-view", "swimlane");
      filterbar.appendChild(treeView);
      filterbar.appendChild(swimView);
      mount.appendChild(filterbar);

      var workbench = createElement("div", "ctv-workbench");
      var treePanel = createElement("section", "ctv-tree-panel");
      var treeToolbar = createElement("div", "ctv-tree-toolbar");
      var treeLabel = createElement("div", "ctv-tree-label");
      treeLabel.appendChild(createElement("strong", "", "Span hierarchy"));
      treeLabel.appendChild(createElement("span", "", "Relative duration"));
      treeToolbar.appendChild(treeLabel);
      var treeActions = createElement("div", "ctv-tree-actions");
      var expandAll = createElement("button", "ctv-button", "展开可见");
      expandAll.type = "button";
      expandAll.setAttribute("data-ctv-action", "expand-all");
      var collapseAll = createElement("button", "ctv-button", "折叠");
      collapseAll.type = "button";
      collapseAll.setAttribute("data-ctv-action", "collapse-all");
      treeActions.appendChild(expandAll);
      treeActions.appendChild(collapseAll);
      treeToolbar.appendChild(treeActions);
      treePanel.appendChild(treeToolbar);
      var treeElement = createElement("div", "ctv-tree");
      treeElement.setAttribute("role", "tree");
      treeElement.setAttribute("aria-label", "Trace spans");
      treePanel.appendChild(treeElement);
      workbench.appendChild(treePanel);
      var swimlanes = createElement("section", "ctv-swimlanes");
      swimlanes.hidden = true;
      workbench.appendChild(swimlanes);
      var detail = createElement("aside", "ctv-detail");
      detail.setAttribute("aria-label", "Selected span details");
      workbench.appendChild(detail);
      mount.appendChild(workbench);
      refreshRows();
      mount.scrollTop = mountScroll;
    }

    function visibleRows() {
      return Array.prototype.slice.call(mount.querySelectorAll(".ctv-node"));
    }

    function selectRow(row, openDetail) {
      if (!row || !state.model) return;
      var id = row.getAttribute("data-ctv-node-id");
      if (!state.model.byId[id]) return;
      state.selectedId = id;
      visibleRows().forEach(function (candidate) {
        var selected = candidate === row;
        candidate.setAttribute("aria-selected", selected ? "true" : "false");
        candidate.tabIndex = selected ? 0 : -1;
      });
      renderDetail(state.model.byId[id]);
      if (openDetail) mount.classList.add("ctv-mobile-detail-open");
    }

    function toggleNode(id, forceExpanded) {
      var node = state.model && state.model.byId[id];
      if (!node || !node.children.length) return;
      if (forceExpanded === true) state.collapsed.delete(id);
      else if (forceExpanded === false) state.collapsed.add(id);
      else if (state.collapsed.has(id)) state.collapsed.delete(id);
      else state.collapsed.add(id);
      refreshRows({ focusId: id });
    }

    function setFilter(filter) {
      if (FILTERS.indexOf(filter) < 0 || !state.model) return;
      state.filter = filter;
      mount.querySelectorAll("[data-ctv-filter]").forEach(function (button) {
        var active = button.getAttribute("data-ctv-filter") === filter;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
      });
      if (filter !== "all") {
        var filteredIds = filterVisibleIds(state.model, filter);
        Object.keys(filteredIds).forEach(function (id) { state.collapsed.delete(id); });
      }
      refreshRows();
    }

    function selectById(id, showTree) {
      if (!state.model || !state.model.byId[id]) return;
      state.selectedId = id;
      var cursor = state.model.byId[id];
      while (cursor) {
        state.collapsed.delete(cursor.id);
        cursor = cursor.parentId ? state.model.byId[cursor.parentId] : null;
      }
      if (showTree === true) {
        state.view = "tree";
        mount.querySelectorAll("[data-ctv-view]").forEach(function (button) {
          var active = button.getAttribute("data-ctv-view") === "tree";
          button.classList.toggle("is-active", active);
          button.setAttribute("aria-pressed", active ? "true" : "false");
        });
      }
      refreshRows({ focusId: showTree === true ? id : null });
      var row = Array.prototype.find.call(mount.querySelectorAll(".ctv-node"), function (candidate) {
        return candidate.getAttribute("data-ctv-node-id") === id;
      });
      if (row) {
        row.scrollIntoView({ block: "center", behavior: "auto" });
        selectRow(row, false);
      } else {
        renderDetail(state.model.byId[id]);
      }
    }

    function cycleNodes(nodes, direction) {
      if (!nodes.length) return;
      var current = nodes.indexOf(state.selectedId);
      var next = current < 0
        ? (direction > 0 ? 0 : nodes.length - 1)
        : (current + direction + nodes.length) % nodes.length;
      selectById(nodes[next], true);
    }

    function refreshSearch(query) {
      state.query = safeText(query, 240);
      state.searchMatches = searchTrace(state.model, state.query);
      state.matchIndex = state.searchMatches.length ? 0 : -1;
      state.searchMatches.forEach(function (match) {
        var cursor = state.model.byId[match.id];
        while (cursor) {
          state.collapsed.delete(cursor.id);
          cursor = cursor.parentId ? state.model.byId[cursor.parentId] : null;
        }
      });
      var count = mount.querySelector(".ctv-search-count");
      if (count) count.textContent = state.query ? state.searchMatches.length + " 个结果" : "未搜索";
      refreshRows();
    }

    function setView(view) {
      if (VIEWS.indexOf(view) < 0) return;
      state.view = view;
      mount.querySelectorAll("[data-ctv-view]").forEach(function (button) {
        var active = button.getAttribute("data-ctv-view") === view;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
      });
      refreshRows();
    }

    function onClick(event) {
      var action = event.target.closest("[data-ctv-action]");
      if (action && mount.contains(action)) {
        var actionName = action.getAttribute("data-ctv-action");
        if (actionName === "detail-back") {
          mount.classList.remove("ctv-mobile-detail-open");
          var selected = mount.querySelector('.ctv-node[aria-selected="true"]');
          if (selected) selected.focus();
          return;
        }
        if (actionName === "expand-all") {
          var visible = filterVisibleIds(state.model, state.filter);
          var expanded = 0;
          state.model.nodes.forEach(function (node) {
            if (visible[node.id] && expanded < MAX_RENDERED_ROWS) {
              state.collapsed.delete(node.id);
              expanded += 1;
            }
          });
          refreshRows();
          return;
        }
        if (actionName === "collapse-all") {
          state.model.nodes.forEach(function (node) {
            if (node.children.length) state.collapsed.add(node.id);
          });
          refreshRows();
          return;
        }
        if (actionName === "toggle-node") {
          event.stopPropagation();
          var toggleRow = action.closest(".ctv-node");
          if (toggleRow) toggleNode(toggleRow.getAttribute("data-ctv-node-id"));
          return;
        }
        if (actionName === "toggle-error-path") {
          state.errorPathOnly = !state.errorPathOnly;
          action.classList.toggle("is-active", state.errorPathOnly);
          action.setAttribute("aria-pressed", state.errorPathOnly ? "true" : "false");
          refreshRows();
          return;
        }
        if (actionName === "previous-error" || actionName === "next-error") {
          cycleNodes(state.model.nodes.filter(function (node) { return node.isError; })
            .map(function (node) { return node.id; }), actionName === "next-error" ? 1 : -1);
          return;
        }
        if (actionName === "previous-match" || actionName === "next-match") {
          if (!state.searchMatches.length) return;
          var direction = actionName === "next-match" ? 1 : -1;
          state.matchIndex = (state.matchIndex + direction + state.searchMatches.length) % state.searchMatches.length;
          selectById(state.searchMatches[state.matchIndex].id, true);
          return;
        }
        if (actionName === "select-swimlane-node") {
          selectById(action.getAttribute("data-ctv-node-id"), false);
          return;
        }
      }
      var filter = event.target.closest("[data-ctv-filter]");
      if (filter && mount.contains(filter)) {
        setFilter(filter.getAttribute("data-ctv-filter"));
        return;
      }
      var view = event.target.closest("[data-ctv-view]");
      if (view && mount.contains(view)) {
        setView(view.getAttribute("data-ctv-view"));
        return;
      }
      var row = event.target.closest(".ctv-node");
      if (row && mount.contains(row)) {
        selectRow(row, true);
      }
    }

    function onInput(event) {
      if (event.target && event.target.getAttribute("data-ctv-search") === "true") {
        var value = safeText(event.target.value, 240);
        state.query = value;
        if (state.searchTimer) global.clearTimeout(state.searchTimer);
        state.searchTimer = global.setTimeout(function () {
          state.searchTimer = null;
          refreshSearch(value);
        }, 120);
      }
    }

    function update(trace) {
      if (state.destroyed) return;
      var nextModel = normalizeTrace(trace);
      if (
        !state.model ||
        !state.model.sessionKey ||
        state.model.sessionKey !== nextModel.sessionKey ||
        !mount.querySelector(".ctv-workbench")
      ) {
        render(trace, false);
        return;
      }
      state.rawTrace = trace;
      state.model = nextModel;
      state.collapsed = new Set(Array.from(state.collapsed).filter(function (id) {
        return Boolean(state.model.byId[id]);
      }));
      if (!state.model.byId[state.selectedId]) {
        state.selectedId = state.model.roots[0] || null;
      }
      state.searchMatches = searchTrace(state.model, state.query);

      var title = mount.querySelector(".ctv-title");
      if (title) title.textContent = state.model.title;
      var status = mount.querySelector(".ctv-status");
      if (status) status.textContent = state.model.status || "unknown";
      var duration = mount.querySelector(".ctv-meta-duration");
      if (duration) duration.textContent = formatDuration(state.model.duration);
      ["turn", "llm", "tool", "agent", "error"].forEach(function (kind) {
        var count = mount.querySelector(".ctv-summary-item--" + kind + " .ctv-summary-count");
        if (count) count.textContent = String(state.model.summary[kind]);
      });
      mount.querySelectorAll("[data-ctv-filter]").forEach(function (button) {
        var filter = button.getAttribute("data-ctv-filter");
        var count = button.querySelector(".ctv-filter-count");
        if (!count) return;
        if (filter === "all") count.textContent = String(state.model.nodes.length);
        else if (filter === "output") count.textContent = String(state.model.summary.llm);
        else count.textContent = String(state.model.summary[filter] || 0);
      });
      var quality = mount.querySelector(".ctv-quality");
      if (quality && quality.parentNode) quality.parentNode.replaceChild(renderQualityPanel(), quality);
      var searchCount = mount.querySelector(".ctv-search-count");
      if (searchCount) {
        searchCount.textContent = state.query ? state.searchMatches.length + " 个结果" : "未搜索";
      }
      refreshRows();
    }

    function onKeydown(event) {
      var row = event.target.closest(".ctv-node");
      if (!row || !mount.contains(row) || !state.model) return;
      var rows = visibleRows();
      var index = rows.indexOf(row);
      var id = row.getAttribute("data-ctv-node-id");
      var node = state.model.byId[id];
      var target = null;
      if (event.key === "ArrowDown") target = rows[Math.min(rows.length - 1, index + 1)];
      else if (event.key === "ArrowUp") target = rows[Math.max(0, index - 1)];
      else if (event.key === "Home") target = rows[0];
      else if (event.key === "End") target = rows[rows.length - 1];
      else if (event.key === "ArrowRight") {
        if (node.children.length && state.collapsed.has(id)) {
          toggleNode(id, true);
          event.preventDefault();
          return;
        }
        if (node.children.length) {
          target = rows[index + 1];
        }
      } else if (event.key === "ArrowLeft") {
        if (node.children.length && !state.collapsed.has(id)) {
          toggleNode(id, false);
          event.preventDefault();
          return;
        }
        if (node.parentId) {
          target = rows.find(function (candidate) {
            return candidate.getAttribute("data-ctv-node-id") === node.parentId;
          });
        }
      } else if (event.key === "Enter" || event.key === " ") {
        selectRow(row, true);
        event.preventDefault();
        return;
      } else {
        return;
      }
      if (target) {
        selectRow(target, false);
        target.focus();
      }
      event.preventDefault();
    }

    mount.addEventListener("click", onClick);
    mount.addEventListener("keydown", onKeydown);
    mount.addEventListener("input", onInput);

    return Object.freeze({
      loading: function (message) { renderState("loading", safeText(message, 320)); },
      empty: function (message) { renderState("empty", safeText(message, 640)); },
      error: function (message) { renderState("error", safeText(message, 1200)); },
      render: render,
      update: update,
      export: function (format) {
        if (!state.model) throw new Error("当前没有可导出的轨迹。");
        return exportTrace(state.model, format);
      },
      getState: function () {
        return {
          filter: state.filter,
          view: state.view,
          collapsed: Array.from(state.collapsed),
          selectedId: state.selectedId,
          query: state.query,
          matchIndex: state.matchIndex,
          errorPathOnly: state.errorPathOnly
        };
      },
      restoreState: function (snapshot) {
        if (!snapshot || typeof snapshot !== "object" || !state.model) return;
        if (FILTERS.indexOf(snapshot.filter) >= 0) state.filter = snapshot.filter;
        if (VIEWS.indexOf(snapshot.view) >= 0) state.view = snapshot.view;
        state.collapsed = new Set(Array.isArray(snapshot.collapsed)
          ? snapshot.collapsed.filter(function (id) { return Boolean(state.model.byId[id]); })
          : []);
        state.selectedId = state.model.byId[snapshot.selectedId] ? snapshot.selectedId : state.selectedId;
        state.query = safeText(snapshot.query, 240);
        state.searchMatches = searchTrace(state.model, state.query);
        state.matchIndex = finiteNumber(snapshot.matchIndex) === null ? -1 : Number(snapshot.matchIndex);
        state.errorPathOnly = snapshot.errorPathOnly === true;
        render(state.rawTrace, true);
      },
      clear: function () {
        if (state.destroyed) return;
        state.model = null;
        state.rawTrace = null;
        state.selectedId = null;
        wipe();
      },
      destroy: function () {
        if (state.destroyed) return;
        if (state.searchTimer) global.clearTimeout(state.searchTimer);
        mount.removeEventListener("click", onClick);
        mount.removeEventListener("keydown", onKeydown);
        mount.removeEventListener("input", onInput);
        if (mount.parentNode) mount.parentNode.removeChild(mount);
        state.destroyed = true;
        state.model = null;
        state.rawTrace = null;
      }
    });
  }

  global.CardsTraceView = Object.freeze({
    create: create,
    _test: Object.freeze({
      normalizeTrace: normalizeTrace,
      computeSummary: computeSummary,
      filterVisibleIds: filterVisibleIds,
      flattenVisible: flattenVisible,
      barGeometry: barGeometry,
      searchTrace: searchTrace,
      pathIds: pathIds,
      buildSwimlanes: buildSwimlanes,
      exportTrace: exportTrace,
      looksSensitive: looksSensitive,
      formatDuration: formatDuration,
      safeText: safeText
    })
  });
})(typeof window !== "undefined" ? window : globalThis);
