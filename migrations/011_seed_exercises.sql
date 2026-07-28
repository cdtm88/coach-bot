-- GYM-03's exercise library, seeded.
--
-- Separate from 010 for the same reason 002 is separate from 001: the schema is
-- structure and this is content, and content gets edited.
--
-- Scoped to what the athlete actually has. `docs/seed/coaching-conversation.md`
-- and `seeds/athlete.json` record a small apartment building gym with limited
-- equipment, resistance bands and glute bands, chosen deliberately because "the
-- programme actually done beats the better programme skipped". A library full of
-- barbell variations would generate sessions he cannot do and GYM-03 would spend
-- its life substituting them away.
--
-- `aliases` is what makes GYM-02 work on constraints written in the athlete's
-- own words. His movement restrictions say "no barbell deadlifts" and "no heavy
-- barbell back squats"; nothing in the system would match those without the
-- names being here.

insert into exercises (name, movement_pattern, equipment, aliases, spinal_load) values
  -- hinge. The whole pattern is withheld pending review around week 8, so every
  -- row here exists to be excluded rather than prescribed. They are present
  -- because GYM-02 can only block a movement the library knows about, and
  -- because the review that releases them needs something to release.
  ('conventional deadlift',   'hinge', 'barbell',   '{"deadlift","conventional deadlift","barbell deadlift"}', 3),
  ('romanian deadlift',       'hinge', 'barbell',   '{"rdl","romanian deadlift"}', 3),
  ('single leg rdl',          'hinge', 'dumbbell',  '{"single leg rdl","single-leg rdl","sl rdl"}', 2),
  ('kettlebell swing',        'hinge', 'kettlebell','{"swing","kb swing","kettlebell swing"}', 3),
  ('hip thrust',              'hinge', 'bodyweight','{"hip thrust","glute bridge"}', 1),

  -- squat
  ('barbell back squat',      'squat', 'barbell',   '{"back squat","barbell squat","squat"}', 3),
  ('goblet squat',            'squat', 'dumbbell',  '{"goblet squat"}', 1),
  ('split squat',             'squat', 'bodyweight','{"split squat","bulgarian split squat"}', 1),
  ('leg press',               'squat', 'machine',   '{"leg press"}', 1),
  ('step up',                 'squat', 'dumbbell',  '{"step up","step-up"}', 1),
  ('wall sit',                'squat', 'bodyweight','{"wall sit"}', 0),

  -- horizontal push
  ('dumbbell bench press',    'push_horizontal', 'dumbbell',  '{"bench press","db bench","dumbbell press"}', 1),
  ('push up',                 'push_horizontal', 'bodyweight','{"push up","press up","pushup"}', 1),
  ('band chest press',        'push_horizontal', 'band',      '{"band press","band chest press"}', 0),

  -- vertical push
  ('dumbbell shoulder press', 'push_vertical', 'dumbbell',  '{"shoulder press","overhead press","ohp"}', 2),
  ('band overhead press',     'push_vertical', 'band',      '{"band overhead press"}', 1),

  -- horizontal pull
  ('single arm dumbbell row', 'pull_horizontal', 'dumbbell',  '{"db row","dumbbell row","single arm row","one arm row"}', 1),
  ('band row',                'pull_horizontal', 'band',      '{"band row","seated band row"}', 0),
  ('inverted row',            'pull_horizontal', 'bodyweight','{"inverted row","body row"}', 1),

  -- vertical pull
  ('lat pulldown',            'pull_vertical', 'machine',   '{"lat pulldown","pulldown"}', 1),
  ('band pulldown',           'pull_vertical', 'band',      '{"band pulldown"}', 0),
  ('assisted pull up',        'pull_vertical', 'bodyweight','{"pull up","pullup","chin up"}', 1),

  -- lunge
  ('walking lunge',           'lunge', 'bodyweight','{"lunge","walking lunge"}', 1),
  ('reverse lunge',           'lunge', 'bodyweight','{"reverse lunge"}', 1),

  -- carry
  ('farmer carry',            'carry', 'dumbbell',  '{"farmer carry","farmers walk","loaded carry"}', 2),
  ('suitcase carry',          'carry', 'dumbbell',  '{"suitcase carry"}', 2),

  -- trunk. The distinction between these three patterns is the whole reason
  -- the trunk is not one pattern: his restrictions rule out flexion and loaded
  -- rotation while anti-extension and anti-rotation are exactly what a repaired
  -- disc wants. Collapsing them into "core" would have banned the useful half.
  ('sit up',                  'core_flexion',       'bodyweight','{"sit up","situp","crunch","crunches"}', 3),
  ('russian twist',           'core_rotation',      'bodyweight','{"russian twist","loaded twist","twist"}', 3),
  ('plank',                   'core_antiextension', 'bodyweight','{"plank","front plank"}', 0),
  ('dead bug',                'core_antiextension', 'bodyweight','{"dead bug","deadbug"}', 0),
  ('side plank',              'core_antirotation',  'bodyweight','{"side plank"}', 0),
  ('pallof press',            'core_antirotation',  'band',      '{"pallof","pallof press"}', 0),
  ('bird dog',                'core_antiextension', 'bodyweight','{"bird dog","birddog"}', 0),

  -- hip and posterior chain isolation, which the physio work sits in (LOG-02)
  ('banded clamshell',        'hip_abduction', 'band',      '{"clamshell","clam shell"}', 0),
  ('banded lateral walk',     'hip_abduction', 'band',      '{"lateral walk","monster walk","crab walk"}', 0),
  ('glute bridge march',      'hip_extension', 'bodyweight','{"glute bridge march","single leg glute bridge"}', 0),
  ('calf raise',              'calf',          'bodyweight','{"calf raise","heel raise"}', 0);
