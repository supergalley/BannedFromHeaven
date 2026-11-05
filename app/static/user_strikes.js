(() => {
  let card;
  let active;
  let pinned = false;
  let hideTimer;
  let controller;

  function closeCard(restoreFocus = false) {
    clearTimeout(hideTimer);
    controller?.abort();
    if (card) card.hidden = true;
    if (active) {
      active.setAttribute('aria-expanded', 'false');
      if (restoreFocus) active.focus();
    }
    active = null;
    pinned = false;
  }

  function positionCard() {
    if (!active || !card) return;
    const rect = active.getBoundingClientRect();
    card.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - card.offsetWidth - 8)) + 'px';
    const below = rect.bottom + 6;
    const top = below + card.offsetHeight < window.innerHeight - 8 ? below : rect.top - card.offsetHeight - 6;
    card.style.top = Math.max(8, top) + 'px';
  }

  async function showCard(badge, pin = false) {
    clearTimeout(hideTimer);
    if (badge === active) {
      pinned = pinned || pin;
      return;
    }
    closeCard();
    active = badge;
    pinned = pin;
    badge.setAttribute('aria-expanded', 'true');
    if (!card) {
      card = document.createElement('div');
      card.id = 'user-strike-card';
      card.className = 'user-strike-card';
      card.setAttribute('role', 'dialog');
      card.setAttribute('aria-label', 'Strike details');
      card.setAttribute('aria-live', 'polite');
      document.body.appendChild(card);
    }
    card.hidden = false;
    card.textContent = 'Loading strike details…';
    positionCard();
    controller = new AbortController();
    const signal = controller.signal;
    try {
      const response = await fetch(badge.dataset.strikeUrl, {
        headers: {Accept: 'application/json'}, cache: 'no-store', signal,
      });
      if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) {
        throw new Error('Could not load strike details.');
      }
      const data = await response.json();
      if (active !== badge || signal.aborted) return;
      card.replaceChildren();
      if (!data.last_strike_at) {
        card.textContent = 'No active strikes.';
      } else {
        const date = document.createElement('div');
        const time = document.createElement('time');
        time.dateTime = data.last_strike_at;
        time.title = 'UTC';
        time.textContent = data.last_strike_label + ' UTC';
        date.append('Last strike: ', time);
        const votes = document.createElement('div');
        votes.textContent = '↓ ' + data.downvotes + '   ↑ ' + data.upvotes;
        votes.title = 'Current non-self votes recorded since the latest active strike; double upvotes count once.';
        votes.setAttribute('aria-label', data.downvotes + ' downvotes and ' + data.upvotes + ' upvotes since the last strike');
        card.append(date, votes);
        badge.dataset.strikeLevel = Math.min(data.active_strikes, 3);
      }
      positionCard();
    } catch (error) {
      if (error.name !== 'AbortError' && active === badge) {
        card.textContent = 'Could not load strike details. Close and reopen to retry.';
        positionCard();
      }
    }
  }

  function scheduleClose() {
    clearTimeout(hideTimer);
    if (!pinned) hideTimer = setTimeout(() => closeCard(), 200);
  }

  document.addEventListener('mouseover', event => {
    const badge = event.target.closest?.('.user-strike-badge');
    if (badge) showCard(badge);
    if (card?.contains(event.target)) clearTimeout(hideTimer);
  });
  document.addEventListener('mouseout', event => {
    if (event.target.closest?.('.user-strike-badge') || card?.contains(event.target)) {
      if (!active?.contains(event.relatedTarget) && !card?.contains(event.relatedTarget)) scheduleClose();
    }
  });
  // Capture clicks so tapping a badge inside a username link opens the card, not the profile.
  document.addEventListener('click', event => {
    const badge = event.target.closest?.('.user-strike-badge');
    if (badge) {
      event.preventDefault();
      event.stopPropagation();
      if (active === badge && pinned) closeCard();
      else showCard(badge, true);
    } else if (!card?.contains(event.target)) closeCard();
  }, true);
  document.addEventListener('focusin', event => {
    if (event.target.matches?.('.user-strike-badge')) showCard(event.target);
  });
  document.addEventListener('focusout', event => {
    if (event.target === active && !card?.contains(event.relatedTarget)) scheduleClose();
  });
  document.addEventListener('keydown', event => {
    const badge = event.target.closest?.('.user-strike-badge');
    if (badge && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      event.stopPropagation();
      if (active === badge && pinned) closeCard();
      else showCard(badge, true);
    } else if (event.key === 'Escape') closeCard(true);
  }, true);
  window.addEventListener('resize', () => closeCard());
  document.addEventListener('scroll', () => closeCard(), true);
})();
