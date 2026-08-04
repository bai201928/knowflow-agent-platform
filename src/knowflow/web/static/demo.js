const BASE = '/api/v1';
let token = '';

function doLogin() {
  const username = document.getElementById('username').value;
  const password = document.getElementById('password').value;
  document.getElementById('login-status').textContent = 'Logging in...';
  setTimeout(() => {
    token = 'demo-token-' + Date.now();
    document.getElementById('login-status').textContent = 'Logged in as ' + username;
    document.getElementById('workflow-panel').style.display = 'block';
    document.getElementById('timeline-panel').style.display = 'block';
    addTimeline('system', 'Connected to KnowFlow demo');
  }, 300);
}

function addTimeline(source, message) {
  const container = document.getElementById('timeline-content');
  const entry = document.createElement('div');
  entry.className = 'timeline-entry';
  const now = new Date().toLocaleTimeString();
  entry.innerHTML = '<span class="time">' + now + '</span> <strong>' + source + '</strong>: ' + message;
  container.appendChild(entry);
  container.scrollTop = container.scrollHeight;
}

function createWorkflow() {
  const text = document.getElementById('request-text').value;
  if (!text) return;
  addTimeline('you', text);
  document.getElementById('request-text').value = '';

  document.getElementById('plan-panel').style.display = 'block';
  document.getElementById('plan-content').textContent = 'Planning...';

  document.getElementById('citation-panel').style.display = 'block';
  document.getElementById('citation-content').textContent = 'Retrieving evidence...';

  document.getElementById('ticket-panel').style.display = 'block';
  document.getElementById('ticket-content').textContent = 'Creating ticket...';

  document.getElementById('approval-panel').style.display = 'block';
  document.getElementById('approval-content').textContent = 'Waiting for approval...';

  setTimeout(() => {
    document.getElementById('plan-content').textContent = '1. Knowledge retrieval: ops-manual-v1\n2. Create P1 ticket\n3. Notify NOC team\n4. Request approval for consumer restart\n5. Execute sandbox restart';
    addTimeline('planner', 'Plan compiled: 5 tasks');

    setTimeout(() => {
      document.getElementById('citation-content').innerHTML = '<div>[1] ops-manual-v1 §3.2 — RocketMQ backlog diagnosis</div><div>[2] ops-manual-v1 §5.1 — Consumer restart procedure</div>';
      addTimeline('retrieval', 'Retrieved 2 evidence segments');

      setTimeout(() => {
        document.getElementById('ticket-content').textContent = 'Ticket TKT-001: RocketMQ orders backlog [P1] [OPEN]\nAssigned to: noc team';
        addTimeline('ticket', 'Ticket TKT-001 created');

        setTimeout(() => {
          document.getElementById('approval-content').textContent = 'Approval #APR-001: Restart orders-consumer\nStatus: WAITING_APPROVAL\nRequired role: APPROVER\n\n[Approve] [Reject]';
          addTimeline('approval', 'Approval #APR-001 pending for consumer restart');
        }, 1000);
      }, 1000);
    }, 1000);
  }, 1500);
}

document.getElementById('password').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') doLogin();
});
