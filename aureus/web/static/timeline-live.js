function fetchTimelineDelta(query, cursor, current, apply, onError = fail) {
  const params = new URLSearchParams(query);
  async function read(after) {
    params.set('since_id', String(after));
    const res = await api('/api/timeline?' + params);
    if (!current()) return;
    if (!res.ok) { if (!res.aborted) onError(res); return; }
    apply(res.data);
    if (res.data.has_more) return read(res.data.since_id);
  }
  return read(cursor);
}

function loadTimelineSince() {
  const session = state.loaded, navigation = pageNavigation, base = String(timelineParams());
  const current = () => state.loaded === session && pageNavigation === navigation &&
    state.page === 'timeline' && !document.hidden && String(timelineParams()) === base;
  if (timelineDelta?.matches(session, navigation, base)) { timelineDelta.again = true; return timelineDelta.promise; }
  const delta = timelineDelta = {again:false, matches:(owner, page, query) => owner === session && page === navigation && query === base};
  async function refresh() {
    if (timelineReading) await timelineReading;
    if (!current()) return;
    if (timelineQuery !== base || timelineCursor == null || !timelineRendered) return loadTimeline(false, false, true, true);
    do {
      delta.again = false;
      await fetchTimelineDelta(base, timelineCursor, current, data => {
        const added = data.records.filter(record => record.id > (tl.records.at(-1)?.id ?? 0));
        const merged = {...data, records:tl.records.concat(added), next_before_id:tl.next};
        applyTimelineData(merged, {added, scroll:'live'});
        cacheWrite('timeline:' + base, merged, TIMELINE_CACHE_MS);
      });
    } while (delta.again && current());
  }
  delta.promise = refresh().finally(() => { if (timelineDelta === delta) timelineDelta = null; });
  return delta.promise;
}

function followOverviewPolling() {
  if (!state.incrementalUpdates) return;
  clearTimeout(overviewSignalTimer);
  if (state.loggedIn && state.page === 'overview' && !document.hidden && !liveSocketOpen()) {
    overviewSignalTimer = setTimeout(refreshOverviewLive, OVERVIEW_SIGNAL_INTERVAL_MS);
  }
}

function refreshOverviewLive(skipRecent = false) {
  const live = overviewLive;
  const current = () => live && live === overviewLive && live.owner === state.loaded &&
    state.page === 'overview' && !document.hidden;
  if (!current()) return;
  if (live.request) { live.again = true; return live.request; }
  clearTimeout(overviewSignalTimer);
  async function sample() {
    do {
      live.again = false;
      const stateRead = api('/api/overview').then(res => {
        if (!current()) return;
        if (notBuilt(res)) {
          $('ov-content').classList.add('hidden');
          $('ov-restart').classList.add('hidden');
          showBanner('ov-missing', NOT_BUILT_TEXT);
        } else if (res.ok) {
          if (JSON.stringify(live.snapshot) !== JSON.stringify(res.data)) renderOverview(res.data);
          live.snapshot = res.data;
          cacheWrite('overview', res.data, OVERVIEW_CACHE_MS);
        } else {
          if (!live.snapshot) showOverviewSignal(null);
          fail(res);
        }
      });
      const query = `hours=24&limit=${OVERVIEW_ROUND_LIMIT}`;
      const apply = data => {
        const before = live.records?.at(-1)?.id ?? 0;
        const records = [...(live.records ?? []), ...data.records.filter(row => row.id > before)].slice(-OVERVIEW_ROUND_LIMIT);
        const changed = !live.records || records.at(-1)?.id !== live.records.at(-1)?.id;
        live.records = records;
        live.cursor = data.since_id ?? records.at(-1)?.id ?? 0;
        const round = {records, since_id:live.cursor};
        const activity = {records:records.slice(-OVERVIEW_ACTIVITY_LIMIT)};
        cacheWrite('overview-round', round, OVERVIEW_CACHE_MS);
        cacheWrite('overview-activity', activity, OVERVIEW_CACHE_MS);
        if (changed) {
          renderOverviewActivity(activity);
          renderOverviewRound(records);
        }
      };
      const recordsRead = skipRecent ? null : live.cursor == null ? api('/api/timeline?' + query).then(res => {
        if (!current()) return;
        if (res.ok) apply(res.data);
        else if (!res.aborted && !notBuilt(res)) fail(res);
      }) : fetchTimelineDelta(query, live.cursor, current, apply);
      skipRecent = false;
      await Promise.all([stateRead, recordsRead]);
    } while (live.again && current());
  }
  live.request = sample().finally(() => {
    live.request = null;
    if (current()) followOverviewPolling();
  });
  return live.request;
}

function followInboxPolling() {
  const activity = inboxActivitySession;
  if (!activity || !state.incrementalUpdates) return;
  clearTimeout(activity.timer);
  if (state.loggedIn && state.page === 'inbox' && !document.hidden && navigator.onLine && !liveSocketOpen()) {
    activity.timer = setTimeout(() => {
      refreshInboxActivity();
      refreshInboxLive();
    }, INBOX_ACTIVITY_REFRESH_MS);
  }
}

function refreshInboxActivity() {
  const activity = inboxActivitySession;
  const current = () => activity && inboxActivitySession === activity && state.page === 'inbox' &&
    !document.hidden && navigator.onLine && sameInboxKey(activity.key, inbox.selected?.key);
  if (!current()) return;
  if (activity.request) { activity.again = true; return activity.request; }
  const query = inboxActivityQuery(inbox.selected);
  function apply(data) {
    const previous = activity.records.at(-1)?.id ?? 0;
    activity.records = activity.records.concat(data.records.filter(row => row.id > previous)).slice(-INBOX_ACTIVITY_LIMIT);
    activity.cursor = data.since_id ?? activity.records.at(-1)?.id ?? 0;
    inboxActivityStatus('实时');
    renderInboxActivity(activity.records, activity);
  }
  function onError(res) {
    if (res.status === 404) clearInboxActivity();
    else inboxActivityStatus(errorText(res));
  }
  async function sample() {
    do {
      activity.again = false;
      if (activity.cursor == null) {
        const res = await api('/api/timeline?' + query);
        if (!current()) return;
        if (res.ok) apply(res.data);
        else if (!res.aborted) onError(res);
      } else {
        await fetchTimelineDelta(query, activity.cursor, current, apply, onError);
      }
    } while (activity.again && current());
  }
  activity.request = sample().finally(() => {
    activity.request = null;
    if (current()) followInboxPolling();
  });
  return activity.request;
}
