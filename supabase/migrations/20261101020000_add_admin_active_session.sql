-- Admin accounts may be signed in on one device at a time. Each admin sign-in stores its
-- session id here; a browser whose session id no longer matches is signed out on its next
-- request ("the newest sign-in wins"). Dentist and technician accounts don't use this column.
alter table public.profiles
    add column if not exists active_session_id text;
