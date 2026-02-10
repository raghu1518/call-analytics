(() => {
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

  const formatTime = (value) => {
    const total = Math.max(0, Number(value || 0));
    const minutes = Math.floor(total / 60);
    const seconds = Math.floor(total % 60);
    return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  };

  const formatDateTime = (date) => {
    if (!(date instanceof Date) || Number.isNaN(date.getTime())) return "--";
    return date.toLocaleString(undefined, {
      weekday: "short",
      year: "numeric",
      month: "long",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
    });
  };

  const parseJSONScript = (id) => {
    const node = document.getElementById(id);
    if (!node) return null;
    try {
      return JSON.parse(node.textContent || "{}");
    } catch (error) {
      return null;
    }
  };

  const escapeHtml = (value) =>
    String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");

  const statusProgress = (status) => {
    const normalized = String(status || "").toLowerCase();
    if (normalized === "completed" || normalized === "failed") return 100;
    if (normalized === "processing") return 60;
    if (normalized === "queued") return 15;
    return 0;
  };

  const setProgress = (progressEl, value) => {
    if (!progressEl) return;
    const bar = progressEl.querySelector(".progress__bar");
    if (!bar) return;
    const safe = clamp(Number(value || 0), 0, 100);
    progressEl.dataset.progress = String(safe);
    bar.style.width = `${safe}%`;
  };

  const updateCallRow = (row, data) => {
    if (!row || !data) return;
    const status = String(data.status || "");
    const statusEl = row.querySelector("[data-status]");
    if (statusEl && status) {
      statusEl.textContent = status;
      statusEl.className = `status status--${status}`;
    }
    setProgress(row.querySelector(".progress"), statusProgress(status));
  };

  const initializeProgressBars = () => {
    document.querySelectorAll(".progress").forEach((progress) => {
      setProgress(progress, Number(progress.dataset.progress || 0));
    });
  };

  const initializeFileInput = () => {
    const input = document.querySelector("[data-file-input]");
    const name = document.querySelector("[data-file-name]");
    if (!input || !name) return;
    input.addEventListener("change", () => {
      name.textContent = input.files?.[0]?.name || "No file selected";
    });
  };

  const initializeSelectAll = () => {
    const selectAll = document.querySelector("[data-select-all]");
    if (!selectAll) return;
    selectAll.addEventListener("change", (event) => {
      const checked = Boolean(event.target.checked);
      document.querySelectorAll("input[name='call_ids']").forEach((checkbox) => {
        checkbox.checked = checked;
      });
    });
  };

  const initializeLivePolling = () => {
    const feed = document.querySelector("[data-live-status]");
    const rows = Array.from(document.querySelectorAll("[data-table-row][data-call-id]"));
    if (!rows.length) {
      const realtimePanel = document.querySelector("[data-supervisor-alerts]");
      if (feed) feed.textContent = realtimePanel ? "Live: realtime standby" : "Live: idle";
      return;
    }

    let timer = null;
    let polling = false;

    const runPoll = async () => {
      if (polling) return;
      polling = true;
      if (feed) feed.textContent = "Live: polling";

      let activeCount = 0;
      await Promise.all(
        rows.map(async (row) => {
          const callId = row.getAttribute("data-call-id");
          if (!callId) return;
          try {
            const response = await fetch(`/api/calls/${callId}`);
            if (!response.ok) return;
            const data = await response.json();
            updateCallRow(row, data);
            const status = String(data.status || "").toLowerCase();
            if (status === "queued" || status === "processing") {
              activeCount += 1;
            }
          } catch (error) {
            return;
          }
        })
      );

      if (feed) {
        feed.textContent =
          activeCount > 0 ? "Live: active monitoring" : "Live: settled";
      }
      if (activeCount === 0 && timer) {
        clearInterval(timer);
        timer = null;
      }
      polling = false;
    };

    runPoll();
    timer = setInterval(runPoll, 5000);
  };

  const initializeCharts = () => {
    if (!window.Chart) return;

    const chartData = parseJSONScript("chart-data");
    const insights = parseJSONScript("insights-data");

    const volumeCanvas = document.getElementById("volumeChart");
    if (volumeCanvas && chartData) {
      new window.Chart(volumeCanvas, {
        type: "bar",
        data: {
          labels: chartData.labels || [],
          datasets: [
            {
              label: "Calls",
              data: chartData.values || [],
              backgroundColor: "rgba(37, 99, 255, 0.78)",
              borderRadius: 8,
            },
          ],
        },
        options: {
          plugins: { legend: { display: false } },
          scales: {
            x: { grid: { display: false } },
            y: { beginAtZero: true },
          },
        },
      });
    }

    const sentimentCanvas = document.getElementById("sentimentChart");
    if (sentimentCanvas && insights?.sentiment) {
      new window.Chart(sentimentCanvas, {
        type: "line",
        data: {
          labels: insights.sentiment.labels || [],
          datasets: [
            {
              label: "Sentiment",
              data: insights.sentiment.values || [],
              borderColor: "rgba(37, 99, 255, 0.95)",
              backgroundColor: "rgba(37, 99, 255, 0.15)",
              fill: true,
              tension: 0.32,
            },
          ],
        },
        options: {
          plugins: { legend: { display: false } },
          scales: { y: { beginAtZero: true, max: 1 } },
        },
      });
    }

    const topicsCanvas = document.getElementById("topicsChart");
    if (topicsCanvas && insights?.topics) {
      new window.Chart(topicsCanvas, {
        type: "bar",
        data: {
          labels: insights.topics.labels || [],
          datasets: [
            {
              label: "Topics",
              data: insights.topics.values || [],
              backgroundColor: "rgba(37, 99, 255, 0.36)",
              borderRadius: 8,
            },
          ],
        },
        options: {
          plugins: { legend: { display: false } },
          scales: {
            x: { grid: { display: false } },
            y: { beginAtZero: true },
          },
        },
      });
    }
  };

  const initializeTableSearch = () => {
    const searchInput = document.querySelector("[data-table-search]");
    const rows = Array.from(document.querySelectorAll("[data-table-row]"));
    if (!searchInput || !rows.length) return;

    const applySearch = () => {
      const query = String(searchInput.value || "").trim().toLowerCase();
      rows.forEach((row) => {
        const text = (row.textContent || "").toLowerCase();
        const match = !query || text.includes(query);
        row.style.display = match ? "" : "none";
      });
    };

    searchInput.addEventListener("input", applySearch);
  };

  const initializeTranscriptSync = () => {
    const audio = document.querySelector(".audio__player");
    const transcript = document.querySelector("[data-transcript]");
    if (!audio || !transcript) return;

    const segments = Array.from(transcript.querySelectorAll(".transcript__segment"));
    if (!segments.length) return;

    let active = -1;
    const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

    const setActive = (index) => {
      if (index === active) return;
      if (active >= 0) segments[active]?.classList.remove("is-active");
      active = index;
      if (active >= 0) {
        const current = segments[active];
        current.classList.add("is-active");
        current.scrollIntoView({
          block: "center",
          behavior: reducedMotion ? "auto" : "smooth",
        });
      }
    };

    audio.addEventListener("timeupdate", () => {
      const current = audio.currentTime;
      const index = segments.findIndex((segment) => {
        const start = Number(segment.dataset.start || 0);
        const end = Number(segment.dataset.end || start);
        return current >= start && current <= end;
      });
      setActive(index);
    });

    transcript.addEventListener("click", (event) => {
      const target = event.target.closest(".transcript__segment");
      if (!target) return;
      const start = Number(target.dataset.start || 0);
      audio.currentTime = start;
      audio.play();
    });
  };

  const initializeTranscriptSearch = () => {
    const input = document.querySelector("[data-transcript-search]");
    const segments = Array.from(
      document.querySelectorAll("[data-transcript] .transcript__segment")
    );
    if (!input || !segments.length) return;

    const applySearch = () => {
      const query = String(input.value || "").trim().toLowerCase();
      segments.forEach((segment) => {
        const text = (segment.textContent || "").toLowerCase();
        segment.style.display = !query || text.includes(query) ? "" : "none";
      });
    };

    input.addEventListener("input", applySearch);
  };

  const initializeRealtimeSupervisorAlerts = () => {
    const root = document.querySelector("[data-supervisor-alerts]");
    if (!root) return;

    const callId = String(root.getAttribute("data-call-id") || "").trim();
    const list = root.querySelector("[data-alert-list]");
    const riskValue = root.querySelector("[data-risk-score]");
    const sentimentValue = root.querySelector("[data-live-sentiment]");
    const connectionBadge = root.querySelector("[data-alert-connection]");
    const globalLive = document.querySelector("[data-live-status]");

    if (!callId || !list) return;

    const alertsById = new Map();
    let reconnectTimer = null;

    const setConnectionState = (state) => {
      if (!connectionBadge) return;
      if (state === "connected") {
        connectionBadge.className = "status status--completed";
        connectionBadge.textContent = "Live";
        if (globalLive) globalLive.textContent = "Live: streaming";
        return;
      }
      if (state === "reconnecting") {
        connectionBadge.className = "status status--processing";
        connectionBadge.textContent = "Reconnecting";
        if (globalLive) globalLive.textContent = "Live: reconnecting";
        return;
      }
      if (state === "offline") {
        connectionBadge.className = "status status--failed";
        connectionBadge.textContent = "Offline";
        if (globalLive) globalLive.textContent = "Live: offline";
        return;
      }
      connectionBadge.className = "status status--queued";
      connectionBadge.textContent = "Standby";
      if (globalLive) globalLive.textContent = "Live: standby";
    };

    const renderAlerts = () => {
      const entries = Array.from(alertsById.values()).sort((a, b) => {
        const left = new Date(a.created_at || 0).getTime();
        const right = new Date(b.created_at || 0).getTime();
        return right - left;
      });

      list.innerHTML = "";
      if (!entries.length) {
        const empty = document.createElement("li");
        empty.className = "muted";
        empty.textContent = "No supervisor alerts yet.";
        list.appendChild(empty);
        return;
      }

      entries.slice(0, 40).forEach((alert) => {
        const item = document.createElement("li");
        const severity = String(alert.severity || "low").toLowerCase();
        const createdAt = new Date(alert.created_at || "");
        item.className = `realtime-alerts__item severity-${severity}`;
        item.innerHTML = `
          <strong>${escapeHtml(String(alert.type || "alert").replace(/_/g, " "))}</strong>
          <p>${escapeHtml(alert.message || "Supervisor attention required.")}</p>
          <small>${escapeHtml(
            formatDateTime(createdAt)
          )} • ${escapeHtml(severity.toUpperCase())}</small>
        `;
        list.appendChild(item);
      });
    };

    const updateMetrics = (riskScore, sentimentScore) => {
      if (riskValue && Number.isFinite(Number(riskScore))) {
        riskValue.textContent = Number(riskScore).toFixed(2);
      }
      if (sentimentValue && Number.isFinite(Number(sentimentScore))) {
        sentimentValue.textContent = Number(sentimentScore).toFixed(2);
      }
    };

    const upsertAlert = (alert) => {
      if (!alert || typeof alert !== "object") return;
      const key = Number(alert.id || 0);
      if (!key) return;
      alertsById.set(key, alert);
      renderAlerts();
    };

    const applySnapshot = (snapshot) => {
      if (!snapshot || typeof snapshot !== "object") return;
      updateMetrics(snapshot.risk_score, snapshot.sentiment_score);
      alertsById.clear();
      const alerts = Array.isArray(snapshot.alerts) ? snapshot.alerts : [];
      alerts.forEach((alert) => {
        const key = Number(alert.id || 0);
        if (key) alertsById.set(key, alert);
      });
      renderAlerts();
    };

    const fetchSnapshot = async () => {
      try {
        const response = await fetch(`/api/realtime/calls/${encodeURIComponent(callId)}/snapshot`);
        if (!response.ok) return;
        const payload = await response.json();
        applySnapshot(payload);
      } catch (error) {
        return;
      }
    };

    const connectStream = () => {
      if (typeof window.EventSource !== "function") {
        setConnectionState("offline");
        return;
      }
      const source = new window.EventSource(
        `/api/realtime/stream?call_id=${encodeURIComponent(callId)}`
      );

      source.onopen = () => {
        setConnectionState("connected");
        if (reconnectTimer) {
          clearTimeout(reconnectTimer);
          reconnectTimer = null;
        }
      };

      source.onmessage = (event) => {
        if (!event || !event.data) return;
        let payload = null;
        try {
          payload = JSON.parse(event.data);
        } catch (error) {
          return;
        }
        if (!payload || typeof payload !== "object") return;
        if (payload.type === "realtime_event") {
          updateMetrics(payload.risk_score, payload.sentiment_score);
          return;
        }
        if (payload.type === "supervisor_alert" && payload.alert) {
          updateMetrics(payload.risk_score);
          upsertAlert(payload.alert);
          return;
        }
        if (payload.type === "supervisor_alert_ack" && payload.alert) {
          upsertAlert(payload.alert);
        }
      };

      source.onerror = () => {
        setConnectionState("reconnecting");
        source.close();
        if (reconnectTimer) clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(() => {
          fetchSnapshot();
          connectStream();
        }, 2500);
      };
    };

    setConnectionState("standby");
    fetchSnapshot();
    connectStream();
  };

  const initializeCallExperience = () => {
    const root = document.querySelector("[data-call-experience]");
    if (!root) return;

    const audio = root.querySelector(".audio__player");
    const canvas = root.querySelector("[data-wave-canvas]");
    const overlay = root.querySelector("[data-wave-overlay]");
    const playhead = root.querySelector("[data-wave-playhead]");
    const playButton = root.querySelector("[data-wave-play]");
    const speedSelect = root.querySelector("[data-speed-select]");
    const progressReadout = root.querySelector("[data-progress-readout]");
    const playbackTime = root.querySelector("[data-playback-time]");
    const recordingEnd = root.querySelector("[data-recording-end]");
    const annotationInput = root.querySelector("[data-annotation-input]");
    const annotationAdd = root.querySelector("[data-annotation-add]");
    const annotationList = root.querySelector("[data-annotation-list]");
    const emotionTrack = root.querySelector("[data-emotion-track]");
    const emotionLegend = root.querySelector("[data-emotion-legend]");
    const miniPlayer = document.querySelector("[data-mini-player]");
    const miniPlay = document.querySelector("[data-mini-play]");
    const miniBack = document.querySelector("[data-mini-back]");
    const miniForward = document.querySelector("[data-mini-forward]");
    const miniTime = document.querySelector("[data-mini-time]");
    const miniSpeed = document.querySelector("[data-mini-speed]");
    const callId = root.getAttribute("data-call-id") || "call";
    const experienceData = parseJSONScript("call-experience-data") || {};
    const aiInsights =
      experienceData.ai_insights && typeof experienceData.ai_insights === "object"
        ? experienceData.ai_insights
        : {};
    const createdAtRaw =
      String(experienceData.recording_start_iso || root.getAttribute("data-created-at") || "");
    const configuredDuration = Number(
      experienceData.duration_seconds || root.getAttribute("data-duration") || 0
    );

    if (!audio || !canvas || !overlay || !playhead) return;
    const sourceNode = audio.querySelector("source");
    const liveAudioSrc = String(audio.getAttribute("data-live-audio-src") || "").trim();
    const liveAudioMetaUrl = String(audio.getAttribute("data-live-audio-meta") || "").trim();
    let lastLiveChunkId = "";
    let pendingLiveRefresh = false;
    let liveAudioMetaPolling = false;
    let liveAudioPollTimer = null;

    const transcriptSegments = Array.isArray(experienceData.transcript_segments)
      ? experienceData.transcript_segments
      : [];
    const backendEvents = Array.isArray(aiInsights.events)
      ? aiInsights.events
      : Array.isArray(experienceData.events)
        ? experienceData.events
        : [];
    const transcriptRows = Array.from(
      document.querySelectorAll("[data-transcript] .transcript__segment")
    );
    const eventList = document.querySelector("[data-event-list]");
    const eventTabContainer = document.querySelector("[data-event-tabs]");
    const insightTabContainer = document.querySelector("[data-insight-tabs]");
    const toneEmoji = {
      positive: "🙂",
      negative: "😟",
      empathetic: "🤝",
      unhelpful: "🙅",
    };
    const toneLabel = {
      positive: "Positive",
      negative: "Negative",
      empathetic: "Empathetic",
      unhelpful: "Unhelpful",
    };

    const storageKey = `call-annotations-${callId}`;
    let annotations = [];
    try {
      annotations = JSON.parse(localStorage.getItem(storageKey) || "[]");
      if (!Array.isArray(annotations)) annotations = [];
    } catch (error) {
      annotations = [];
    }

    const saveAnnotations = () => {
      try {
        localStorage.setItem(storageKey, JSON.stringify(annotations));
      } catch (error) {
        return;
      }
    };

    const getDuration = () => {
      if (Number.isFinite(audio.duration) && audio.duration > 0) return audio.duration;
      return configuredDuration > 0 ? configuredDuration : 0;
    };

    const backendEndDisplay = String(experienceData.recording_end_display || "").trim();

    const buildLiveAudioUrl = (cacheKey) => {
      if (!liveAudioSrc) return "";
      const base = `${liveAudioSrc}?fallback=1`;
      const key = String(cacheKey || Date.now());
      return `${base}&t=${encodeURIComponent(key)}`;
    };

    const refreshLiveAudioSource = (cacheKey) => {
      const liveUrl = buildLiveAudioUrl(cacheKey);
      if (!liveUrl) return;
      const seekTime = Number(audio.currentTime || 0);
      const wasPlaying = !audio.paused;
      const rate = Number(audio.playbackRate || 1);
      if (sourceNode) sourceNode.src = liveUrl;
      else audio.src = liveUrl;
      audio.load();
      audio.addEventListener(
        "loadedmetadata",
        () => {
          const maxTime = Number.isFinite(audio.duration) ? audio.duration : seekTime;
          audio.currentTime = clamp(seekTime, 0, maxTime || seekTime || 0);
          audio.playbackRate = Number.isFinite(rate) && rate > 0 ? rate : 1;
          if (wasPlaying) {
            audio.play().catch(() => {});
          }
        },
        { once: true }
      );
    };

    const pollLiveAudioMeta = async () => {
      if (!liveAudioMetaUrl || liveAudioMetaPolling) return;
      liveAudioMetaPolling = true;
      try {
        const response = await fetch(liveAudioMetaUrl, { cache: "no-store" });
        if (!response.ok) return;
        const payload = await response.json();
        const liveAudio = payload && typeof payload === "object" ? payload.live_audio : null;
        if (!liveAudio || typeof liveAudio !== "object") return;
        const available = Boolean(liveAudio.available);
        const nextChunkId = String(liveAudio.last_chunk_id || "").trim();
        if (!available || !nextChunkId || nextChunkId === lastLiveChunkId) return;
        lastLiveChunkId = nextChunkId;
        if (audio.paused || audio.ended) {
          refreshLiveAudioSource(nextChunkId);
          pendingLiveRefresh = false;
        } else {
          pendingLiveRefresh = true;
        }
      } catch (error) {
        return;
      } finally {
        liveAudioMetaPolling = false;
      }
    };

    const updateProgress = () => {
      const current = Number(audio.currentTime || 0);
      const duration = getDuration();
      const progressText = `⏱️ ${formatTime(current)} / ${formatTime(duration)}`;
      if (progressReadout) progressReadout.textContent = progressText;
      if (playbackTime) playbackTime.textContent = formatTime(current);
      if (miniTime) miniTime.textContent = `${formatTime(current)} / ${formatTime(duration)}`;

      const ratio = duration > 0 ? clamp(current / duration, 0, 1) : 0;
      playhead.style.left = `${ratio * 100}%`;
    };

    const updateMiniPlayerState = () => {
      if (!miniPlay) return;
      miniPlay.textContent = audio.paused ? "▶️" : "⏸️";
    };

    const updateRecordingEnd = () => {
      if (!recordingEnd) return;
      if (backendEndDisplay && backendEndDisplay !== "--" && getDuration() <= 0) {
        recordingEnd.textContent = backendEndDisplay;
        return;
      }
      const start = new Date(createdAtRaw || "");
      const duration = getDuration();
      if (!Number.isNaN(start.getTime()) && duration > 0) {
        const end = new Date(start.getTime() + duration * 1000);
        recordingEnd.textContent = formatDateTime(end);
      } else {
        recordingEnd.textContent = "--";
      }
    };

    const seekTo = (seconds) => {
      const duration = getDuration();
      if (duration <= 0) return;
      audio.currentTime = clamp(seconds, 0, duration);
      if (audio.paused) audio.play();
    };

    const clearOverlayMarkers = () => {
      overlay.querySelectorAll(".wave-marker").forEach((node) => node.remove());
    };

    const createMarker = (time, className, title) => {
      const duration = getDuration();
      if (duration <= 0) return;
      const marker = document.createElement("button");
      marker.type = "button";
      marker.className = `wave-marker ${className || ""}`.trim();
      marker.style.left = `${clamp((time / duration) * 100, 0, 100)}%`;
      marker.title = title || formatTime(time);
      marker.addEventListener("click", () => seekTo(time));
      overlay.appendChild(marker);
    };

    const renderMarkers = () => {
      clearOverlayMarkers();
      const segmentTimes = transcriptSegments
        .map((segment) => Number(segment.start || 0))
        .filter((value) => Number.isFinite(value))
        .slice(0, 80);
      segmentTimes.forEach((time) => createMarker(time, "", `Transcript marker @ ${formatTime(time)}`));

      annotations.forEach((annotation) => {
        createMarker(
          Number(annotation.time || 0),
          "wave-marker--annotation",
          `${annotation.text || "Annotation"} @ ${formatTime(annotation.time)}`
        );
      });
    };

    const renderAnnotations = () => {
      if (!annotationList) return;
      annotationList.innerHTML = "";
      if (!annotations.length) {
        const empty = document.createElement("li");
        empty.innerHTML = "<span class='muted'>No annotations yet.</span>";
        annotationList.appendChild(empty);
        return;
      }

      annotations
        .sort((a, b) => Number(a.time || 0) - Number(b.time || 0))
        .forEach((annotation) => {
          const li = document.createElement("li");
          const jump = document.createElement("button");
          jump.type = "button";
          jump.textContent = `${formatTime(annotation.time)} - ${annotation.text}`;
          jump.addEventListener("click", () => seekTo(Number(annotation.time || 0)));
          li.appendChild(jump);
          annotationList.appendChild(li);
        });
    };

    const drawPlaceholderWave = () => {
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      const width = canvas.clientWidth || 640;
      const height = canvas.clientHeight || 120;
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#f8fafc";
      ctx.fillRect(0, 0, width, height);
      ctx.strokeStyle = "rgba(100,116,139,0.55)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (let x = 0; x < width; x += 3) {
        const noise = Math.sin(x / 17) * 12 + Math.sin(x / 37) * 8;
        const y = height / 2 + noise * 0.35;
        if (x === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.stroke();
    };

    const drawWaveform = async () => {
      const src = audio.querySelector("source")?.src || audio.currentSrc;
      if (!src) {
        drawPlaceholderWave();
        return;
      }
      const ctx = canvas.getContext("2d");
      if (!ctx) return;

      const width = canvas.clientWidth || 640;
      const height = canvas.clientHeight || 120;
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      try {
        const response = await fetch(src);
        if (!response.ok) throw new Error("audio fetch failed");
        const buffer = await response.arrayBuffer();
        const audioContext = new (window.AudioContext || window.webkitAudioContext)();
        const decoded = await audioContext.decodeAudioData(buffer.slice(0));
        const channel = decoded.getChannelData(0);
        const bucketCount = width;
        const bucketSize = Math.max(1, Math.floor(channel.length / bucketCount));
        const amplitudes = new Array(bucketCount).fill(0);

        for (let i = 0; i < bucketCount; i += 1) {
          const start = i * bucketSize;
          let sum = 0;
          for (let j = 0; j < bucketSize; j += 1) {
            sum += Math.abs(channel[start + j] || 0);
          }
          amplitudes[i] = sum / bucketSize;
        }

        const maxAmp = Math.max(...amplitudes, 0.001);
        ctx.clearRect(0, 0, width, height);
        ctx.fillStyle = "#f8fafc";
        ctx.fillRect(0, 0, width, height);
        ctx.fillStyle = "rgba(100,116,139,0.78)";

        const mid = height / 2;
        for (let x = 0; x < bucketCount; x += 1) {
          const value = amplitudes[x] / maxAmp;
          const bar = Math.max(1, value * (height * 0.44));
          ctx.fillRect(x, mid - bar, 1, bar * 2);
        }

        if (audioContext.state !== "closed") {
          audioContext.close().catch(() => {});
        }
      } catch (error) {
        drawPlaceholderWave();
      }
    };

    let events = backendEvents.length
      ? backendEvents.map((event, index) => ({
          index: Number(event.index || index + 1),
          start: Number(event.start || 0),
          label: String(event.topic || event.label || event.title || "General"),
          tone: String(event.tone || "positive").toLowerCase(),
          speaker: String(event.speaker || ""),
          excerpt: String(event.excerpt || event.detail || ""),
          confidence: Number(event.confidence || 0),
          evidence: String(event.evidence || "context"),
        }))
      : transcriptSegments
          .filter((segment) => segment && segment.text)
          .slice(0, 120)
          .map((segment, index) => ({
            index: index + 1,
            start: Number(segment.start || 0),
            label: "General",
            tone: "positive",
            speaker: String(segment.speaker || ""),
            excerpt: String(segment.text || ""),
            confidence: 0.55,
            evidence: "transcript_fallback",
          }));

    if (!events.length && transcriptRows.length) {
      events = transcriptRows.slice(0, 120).map((row, index) => {
        const speaker = row.querySelector(".transcript__speaker")?.textContent || "";
        const excerpt = row.querySelector(".transcript__text")?.textContent || row.textContent || "";
        return {
          index: index + 1,
          start: Number(row.dataset.start || 0),
          label: "General",
          tone: "positive",
          speaker: String(speaker).trim(),
          excerpt: String(excerpt).trim(),
          confidence: 0.5,
          evidence: "dom_transcript",
        };
      });
    }

    const rawEmotionMarkers = Array.isArray(experienceData.player_emotions)
      ? experienceData.player_emotions
      : [];
    const fallbackEmotionMarkers = events.map((event) => ({
      time: Number(event.start || 0),
      tone: event.tone || "positive",
      emoji: toneEmoji[event.tone] || "🙂",
      label: event.label || "Conversation",
    }));
    const markerSource = rawEmotionMarkers.length ? rawEmotionMarkers : fallbackEmotionMarkers;
    const normalizedMarkers = markerSource
      .map((marker) => ({
        time: Number(marker.time || marker.start || 0),
        tone: String(marker.tone || "positive").toLowerCase(),
        emoji:
          String(marker.emoji || "").trim() ||
          toneEmoji[String(marker.tone || "positive").toLowerCase()] ||
          "🙂",
        label: String(marker.label || marker.topic || "Conversation"),
      }))
      .filter((marker) => Number.isFinite(marker.time) && marker.time >= 0)
      .sort((a, b) => a.time - b.time);
    const spacingHint = configuredDuration > 0 ? Math.max(3, configuredDuration / 14) : 3.5;
    const emotionMarkers = [];
    normalizedMarkers.forEach((marker) => {
      const previous = emotionMarkers[emotionMarkers.length - 1];
      if (!previous || marker.time - previous.time >= spacingHint) {
        emotionMarkers.push(marker);
      }
    });

    const renderEmotionTrack = () => {
      if (!emotionTrack) return;
      const duration = getDuration();
      emotionTrack.innerHTML = "";

      if (!emotionMarkers.length) {
        if (emotionLegend) {
          emotionLegend.innerHTML = `<span class='muted'>No emotion markers available for this call (${events.length} events detected).</span>`;
        }
        return;
      }

      const visibleMarkers = emotionMarkers.slice(0, 18);
      const maxTime = visibleMarkers.reduce((max, marker) => Math.max(max, marker.time), 0);
      const effectiveDuration = duration > 0 ? duration : maxTime;
      const spreadByIndex = effectiveDuration <= 0;

      visibleMarkers.forEach((marker, index) => {
        const ratio = spreadByIndex
          ? visibleMarkers.length === 1
            ? 0.5
            : index / (visibleMarkers.length - 1)
          : clamp(marker.time / effectiveDuration, 0, 1);
        const markerButton = document.createElement("button");
        markerButton.type = "button";
        markerButton.className = `emotion-marker tone-${marker.tone}`;
        markerButton.style.left = `${ratio * 100}%`;
        markerButton.title = `${marker.label} (${toneLabel[marker.tone] || "Tone"}) @ ${formatTime(
          marker.time
        )}`;
        markerButton.textContent = marker.emoji;
        markerButton.addEventListener("click", () => seekTo(marker.time));
        emotionTrack.appendChild(markerButton);
      });

      if (emotionLegend) {
        const uniqueTones = [];
        emotionMarkers.forEach((marker) => {
          if (!uniqueTones.includes(marker.tone)) uniqueTones.push(marker.tone);
        });
        emotionLegend.innerHTML = uniqueTones
          .map((tone) => {
            const emoji = toneEmoji[tone] || "🙂";
            const label = toneLabel[tone] || "Positive";
            return `<span class="emotion-pill tone-${tone}">${emoji} ${escapeHtml(label)}</span>`;
          })
          .join("");
      }
    };

    const renderEvents = (tone) => {
      if (!eventList) return;
      const selected = tone || "all";
      const filtered =
        selected === "all" ? events : events.filter((event) => event.tone === selected);
      eventList.innerHTML = "";

      if (!filtered.length) {
        const empty = document.createElement("p");
        empty.className = "muted";
        empty.textContent = "No events in this filter.";
        eventList.appendChild(empty);
        return;
      }

      filtered.forEach((event) => {
        const emoji = toneEmoji[event.tone] || "🔵";
        const speaker = event.speaker ? event.speaker : "Conversation";
        const excerpt = event.excerpt || "No additional event detail available.";
        const confidence = Number.isFinite(Number(event.confidence))
          ? clamp(Number(event.confidence), 0, 0.99)
          : 0;
        const confidenceLabel = confidence > 0 ? `${confidence.toFixed(2)} conf` : "conf n/a";
        const evidenceLabel = event.evidence ? String(event.evidence).replace(/_/g, " ") : "context";
        const button = document.createElement("button");
        button.type = "button";
        button.className = `event-item tone-${event.tone}`;
        button.innerHTML = `
          <span class="event-item__index">${emoji} ${event.index}</span>
          <span class="event-item__main">
            <span class="event-item__topic">${escapeHtml(event.label)}</span>
            <span class="event-item__chips">
              <span class="event-chip event-chip--confidence">${escapeHtml(confidenceLabel)}</span>
              <span class="event-chip">${escapeHtml(evidenceLabel)}</span>
            </span>
            <span class="event-item__excerpt">${escapeHtml(excerpt)}</span>
            <span class="event-item__meta">${escapeHtml(speaker)} - ${formatTime(event.start)}</span>
          </span>
          <span class="event-item__time">${formatTime(event.start)}</span>
        `;
        button.title = excerpt;
        button.addEventListener("click", () => {
          seekTo(event.start);
          const target = transcriptRows.find(
            (row) => Math.abs(Number(row.dataset.start || 0) - event.start) <= 0.6
          );
          if (target) {
            target.classList.add("is-active");
            target.scrollIntoView({ block: "center", behavior: "smooth" });
          }
        });
        eventList.appendChild(button);
      });
    };

    if (eventTabContainer) {
      const tabButtons = Array.from(eventTabContainer.querySelectorAll("[data-event-filter]"));
      tabButtons.forEach((button) => {
        button.addEventListener("click", () => {
          tabButtons.forEach((tab) => tab.classList.remove("is-active"));
          button.classList.add("is-active");
          renderEvents(button.getAttribute("data-event-filter"));
        });
      });
    }

    if (insightTabContainer) {
      const tabs = Array.from(insightTabContainer.querySelectorAll("[data-insight-tab]"));
      const panes = Array.from(document.querySelectorAll("[data-insight-pane]"));
      const activatePane = (tabName) => {
        tabs.forEach((tab) => {
          tab.classList.toggle("is-active", tab.getAttribute("data-insight-tab") === tabName);
        });
        panes.forEach((pane) => {
          pane.classList.toggle("is-active", pane.getAttribute("data-insight-pane") === tabName);
        });
      };

      tabs.forEach((tab) => {
        tab.addEventListener("click", () => {
          activatePane(tab.getAttribute("data-insight-tab"));
        });
      });

      const defaultTab =
        tabs.find((tab) => tab.classList.contains("is-active"))?.getAttribute("data-insight-tab") ||
        tabs[0]?.getAttribute("data-insight-tab");
      if (defaultTab) activatePane(defaultTab);
    }

    const togglePlayback = () => {
      if (audio.paused) audio.play();
      else audio.pause();
    };

    const setPlaybackRate = (value) => {
      const next = Number(value || 1);
      const safe = Number.isFinite(next) && next > 0 ? next : 1;
      audio.playbackRate = safe;
      const rateString = String(safe);
      if (speedSelect && speedSelect.value !== rateString) speedSelect.value = rateString;
      if (miniSpeed && miniSpeed.value !== rateString) miniSpeed.value = rateString;
    };

    if (miniPlayer) {
      const updateMiniVisibility = (visible) => {
        miniPlayer.classList.toggle("is-visible", visible);
      };
      if ("IntersectionObserver" in window) {
        const observer = new IntersectionObserver(
          (entries) => {
            entries.forEach((entry) => {
              updateMiniVisibility(!entry.isIntersecting);
            });
          },
          { threshold: 0.16 }
        );
        observer.observe(root);
      } else {
        const fallbackVisibility = () => {
          const rect = root.getBoundingClientRect();
          updateMiniVisibility(rect.bottom < 80);
        };
        fallbackVisibility();
        window.addEventListener("scroll", fallbackVisibility, { passive: true });
      }
    }

    if (miniPlay) {
      miniPlay.addEventListener("click", togglePlayback);
    }
    if (miniBack) {
      miniBack.addEventListener("click", () => {
        seekTo(Number(audio.currentTime || 0) - 10);
      });
    }
    if (miniForward) {
      miniForward.addEventListener("click", () => {
        seekTo(Number(audio.currentTime || 0) + 10);
      });
    }
    if (miniSpeed) {
      miniSpeed.addEventListener("change", () => {
        setPlaybackRate(miniSpeed.value);
      });
    }

    const isTypingContext = (target) => {
      if (!target || !(target instanceof HTMLElement)) return false;
      const tag = target.tagName;
      return (
        tag === "INPUT" ||
        tag === "TEXTAREA" ||
        tag === "SELECT" ||
        Boolean(target.isContentEditable)
      );
    };

    document.addEventListener("keydown", (event) => {
      if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;
      const key = String(event.key || "").toLowerCase();
      const typing = isTypingContext(document.activeElement);
      if (typing && key !== "n") return;

      if (key === " " || key === "k") {
        event.preventDefault();
        togglePlayback();
        return;
      }
      if (key === "j") {
        event.preventDefault();
        seekTo(Number(audio.currentTime || 0) - 10);
        return;
      }
      if (key === "l") {
        event.preventDefault();
        seekTo(Number(audio.currentTime || 0) + 10);
        return;
      }
      if (key === "n" && annotationInput) {
        event.preventDefault();
        annotationInput.focus();
      }
    });

    audio.addEventListener("timeupdate", updateProgress);
    audio.addEventListener("loadedmetadata", () => {
      updateProgress();
      updateRecordingEnd();
      renderMarkers();
      renderEmotionTrack();
      drawWaveform();
      updateMiniPlayerState();
    });
    audio.addEventListener("play", () => {
      if (playButton) playButton.textContent = "⏸️ Pause";
      updateMiniPlayerState();
    });
    audio.addEventListener("pause", () => {
      if (playButton) playButton.textContent = "▶️ Play";
      updateMiniPlayerState();
      if (pendingLiveRefresh) {
        pendingLiveRefresh = false;
        refreshLiveAudioSource(lastLiveChunkId);
      }
    });

    if (playButton) {
      playButton.addEventListener("click", togglePlayback);
    }

    if (speedSelect) {
      speedSelect.addEventListener("change", () => {
        setPlaybackRate(speedSelect.value);
      });
    }

    if (annotationAdd && annotationInput) {
      annotationAdd.addEventListener("click", () => {
        const text = String(annotationInput.value || "").trim();
        if (!text) return;
        annotations.push({
          id: Date.now(),
          time: Number(audio.currentTime || 0),
          text,
        });
        annotationInput.value = "";
        saveAnnotations();
        renderMarkers();
        renderAnnotations();
      });
    }

    updateProgress();
    updateRecordingEnd();
    renderMarkers();
    renderEmotionTrack();
    renderAnnotations();
    renderEvents("all");
    setPlaybackRate(speedSelect?.value || miniSpeed?.value || "1");
    updateMiniPlayerState();
    drawWaveform();
    window.addEventListener("resize", drawWaveform);

    if (liveAudioMetaUrl) {
      pollLiveAudioMeta();
      liveAudioPollTimer = window.setInterval(pollLiveAudioMeta, 6000);
      window.addEventListener("beforeunload", () => {
        if (liveAudioPollTimer) window.clearInterval(liveAudioPollTimer);
      });
    }
  };

  initializeProgressBars();
  initializeFileInput();
  initializeSelectAll();
  initializeLivePolling();
  initializeCharts();
  initializeTableSearch();
  initializeTranscriptSync();
  initializeTranscriptSearch();
  initializeRealtimeSupervisorAlerts();
  initializeCallExperience();
})();
