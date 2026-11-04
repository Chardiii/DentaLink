-- Proof of consent: which version of the Terms and Conditions and Data Privacy Notice each user
-- accepted at registration, and when. Accounts created before this change have null in both.
alter table public.profiles
    add column if not exists terms_version text,
    add column if not exists terms_accepted_at timestamptz;
