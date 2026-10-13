BEGIN IMMEDIATE;

INSERT INTO admins (
  admin_level,
  iso_code,
  parent_admin,
  name,
  name_en,
  drive_on_right,
  allow_intersection_names,
  default_language,
  supported_languages,
  geom
)
SELECT
  2,
  'US',
  NULL,
  'United States',
  'United States',
  1,
  0,
  'en',
  'en',
  CastToMultiPolygon(ST_UnaryUnion(ST_Collect(geom)))
FROM admins
WHERE admin_level = 4;

UPDATE admins
SET parent_admin = (
  SELECT rowid
  FROM admins
  WHERE admin_level = 2 AND iso_code = 'US'
)
WHERE admin_level = 4;

COMMIT;
