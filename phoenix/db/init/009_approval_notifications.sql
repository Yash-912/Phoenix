-- Which Slack message belongs to which approval request (the Slack bridge).
--
-- This is a separate table, not columns on approvals, because 007 says the only
-- UPDATE the application runs on approvals is the one transition out of 'pending'
-- and that nothing rewrites a decided row. Slack bookkeeping changes after a
-- decision (the message is edited and then marked settled), so it lives here.
--
-- One row per approval: the bridge records the message it posted, and a restart
-- reads this table instead of posting the same request again. settled_at is set
-- once, when the message has been edited to show the outcome. A crash between
-- posting and recording leaves no row, so the next pass posts again. That
-- duplicate is accepted: both messages carry the same approval id and the first
-- answer wins.
CREATE TABLE approval_notifications (
    approval_id BIGINT PRIMARY KEY REFERENCES approvals(id),
    channel_id TEXT NOT NULL,
    message_ts TEXT NOT NULL,
    posted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at TIMESTAMPTZ
);
