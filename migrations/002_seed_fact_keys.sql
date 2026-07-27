-- P00: the controlled key vocabulary of docs/memory-design.md section 5.
--
-- Half lives per namespace: profile 365, goal 90, availability 30, prefs 120,
-- equipment 180, physiology 42. Constraint keys never decay (SAFE-03).
--
-- Anything outside this vocabulary is rejected at write time by the foreign key
-- on facts (MEM-01). Adding a key is a migration, deliberately, so the model
-- cannot widen its own namespace.

insert into fact_keys (key, category, value_type, decay_days, safety) values
  -- constraint.*  safety constrained, stated only, never decays (SAFE-01/03)
  ('constraint.movement_restrictions', 'constraint',   'list',   null, true),
  ('constraint.injury_history',        'constraint',   'list',   null, true),
  ('constraint.medical_flags',         'constraint',   'list',   null, true),

  -- profile.*
  ('profile.height_cm',                'profile',      'number',  365, false),
  ('profile.birth_year',               'profile',      'number',  365, false),
  ('profile.primary_sport',            'profile',      'text',    365, false),
  ('profile.training_age_years',       'profile',      'number',  365, false),

  -- goal.*
  ('goal.target_weight_kg',            'goal',         'number',   90, false),
  ('goal.protein_target_g',            'goal',         'number',   90, false),
  ('goal.event_target',                'goal',         'text',     90, false),
  ('goal.milestone_dates',             'goal',         'list',     90, false),
  ('goal.fitness_preservation',        'goal',         'text',     90, false),

  -- availability.*  stated then observed (design section 8)
  ('availability.days',                'availability', 'list',     30, false),
  ('availability.weekday_minutes',     'availability', 'number',   30, false),
  ('availability.weekend_minutes',     'availability', 'number',   30, false),
  ('availability.blackouts',           'availability', 'list',     30, false),

  -- prefs.*
  ('prefs.disliked_sessions',          'prefs',        'list',    120, false),
  ('prefs.notification_times',         'prefs',        'object',  120, false),
  ('prefs.coach_tone',                 'prefs',        'text',    120, false),

  -- equipment.*
  ('equipment.trainer',                'equipment',    'text',    180, false),
  ('equipment.bikes',                  'equipment',    'list',    180, false),
  ('equipment.gym_access',             'equipment',    'text',    180, false),

  -- physiology.*  computed or inferred; a ramp test supersedes silently
  -- (CONS-05, BLOCK-06)
  ('physiology.ftp_watts',             'physiology',   'number',   42, false),
  ('physiology.max_hr',                'physiology',   'number',   42, false),
  ('physiology.threshold_hr',          'physiology',   'number',   42, false);

-- OBS-05: the five inbound feeds carrying a staleness threshold, with the
-- thresholds from docs/memory-design.md section 10.
insert into feeds (name, stale_after_hours) values
  ('activities',  168),   -- 7d
  ('fit_archive', 168),   -- 7d
  ('wellness',     48),
  ('body_mass',   288),   -- 12d, and the only weigh in prompt (HLTH-15)
  ('calendar',     24);
