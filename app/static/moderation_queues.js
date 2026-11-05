(() => {
  const queues = document.getElementById('mod-room-queues');
  if (!queues) return;
  const status = document.getElementById('mod-queue-status');
  const refresh = document.getElementById('refresh-mod-queues');
  let busy = false;

  function updateQueues(result) {
    for (const [kind, html] of Object.entries(result.queues)) {
      const current = queues.querySelector('[data-queue="' + kind + '"]');
      const template = document.createElement('template');
      template.innerHTML = html.trim();
      const next = template.content.firstElementChild;
      // Keep a draft rejection reason or playing video when the item did not change.
      if (current.dataset.itemId === next.dataset.itemId) {
        current.querySelector('.queue-heading').replaceWith(next.querySelector('.queue-heading'));
      } else {
        current.replaceWith(next);
      }
    }
    const counts = result.notifications;
    const nav = document.getElementById('mod-room-nav');
    if (nav) {
      nav.classList.toggle('moderator-review-glow', counts.pending_review_count > 0 || counts.pending_dupe_count > 0);
      const counter = document.getElementById('mod-room-nav-count');
      counter.textContent = (counts.pending_review_count ? ' (' + counts.pending_review_count + ')' : '') +
        (counts.pending_dupe_count ? ' · 👯' + counts.pending_dupe_count : '');
      const url = new URL(nav.href);
      url.hash = counts.pending_review_count ? 'review-queue' : counts.pending_dupe_count ? 'duplicates-queue' : '';
      nav.href = url.href;
    }
  }

  async function requestQueues(url, options = {}) {
    if (busy) return;
    busy = true;
    const controls = [...document.querySelectorAll('#mod-room-queues button:not(:disabled), #mod-room-queues select:not(:disabled), [data-mod-queue-action] button:not(:disabled), #refresh-mod-queues')];
    controls.forEach(control => { control.disabled = true; });
    queues.setAttribute('aria-busy', 'true');
    status.classList.remove('text-warning');
    status.textContent = 'Updating queues…';
    try {
      const response = await fetch(url, {
        ...options,
        cache: 'no-store',
        headers: {Accept: 'application/json'},
      });
      if (!response.headers.get('content-type')?.includes('application/json')) {
        throw new Error('Could not update the queue. Please try again or sign in again.');
      }
      const result = await response.json();
      if (result.queues) updateQueues(result);
      if (!response.ok && response.status !== 409) {
        throw new Error(result.error || 'Could not update the queue. Please try again.');
      }
      status.textContent = result.message || 'Queues refreshed.';
    } catch (error) {
      status.classList.add('text-warning');
      status.textContent = error.message || 'Could not update the queue. Please try again.';
    } finally {
      controls.forEach(control => { control.disabled = false; });
      queues.removeAttribute('aria-busy');
      busy = false;
    }
  }

  document.addEventListener('submit', event => {
    const form = event.target.closest('[data-mod-queue-action]');
    if (!form) return;
    event.preventDefault();
    if (busy || !form.reportValidity()) return;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;
    requestQueues(form.action, {method: 'POST', body: new FormData(form)});
  });
  refresh.addEventListener('click', () => requestQueues(refresh.dataset.url));
})();
