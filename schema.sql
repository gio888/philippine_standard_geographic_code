-- Enable PostGIS for spatial support (run once per database).
CREATE EXTENSION IF NOT EXISTS postgis;

-- Reference table for geographic levels (Region, Province, City, etc.).
CREATE TABLE IF NOT EXISTS geographic_levels (
    level_code TEXT PRIMARY KEY,
    description TEXT NOT NULL
);

INSERT INTO geographic_levels (level_code, description) VALUES
    ('Reg', 'Region'),
    ('Prov', 'Province'),
    ('City', 'City'),
    ('Mun', 'Municipality'),
    ('SubMun', 'Sub-municipality / district'),
    ('Bgy', 'Barangay'),
    ('Other', 'Special PSGC aggregate')
ON CONFLICT (level_code) DO NOTHING;

-- City classification reference (Component, Highly Urbanized, Independent Component).
CREATE TABLE IF NOT EXISTS city_class_types (
    class_code TEXT PRIMARY KEY,
    description TEXT NOT NULL
);

INSERT INTO city_class_types (class_code, description) VALUES
    ('CC', 'Component City'),
    ('HUC', 'Highly Urbanized City'),
    ('ICC', 'Independent Component City')
ON CONFLICT (class_code) DO NOTHING;

-- Income class enumeration per DOF Department Order.
CREATE TABLE IF NOT EXISTS income_brackets (
    bracket_code TEXT PRIMARY KEY,
    description TEXT NOT NULL
);

INSERT INTO income_brackets (bracket_code, description) VALUES
    ('1st', 'Average annual income ≥ ₱1B'),
    ('2nd', '₱800M - < ₱1B'),
    ('3rd', '₱650M - < ₱800M'),
    ('4th', '₱500M - < ₱650M'),
    ('5th', '₱350M - < ₱500M'),
    ('6th', '₱250M - < ₱350M'),
    ('2nd*', 'Transitional 2nd class (pending DOF validation)'),
    ('3rd*', 'Transitional 3rd class (pending DOF validation)'),
    ('4th*', 'Transitional 4th class (pending DOF validation)'),
    ('-', 'Not classified / pending')
ON CONFLICT (bracket_code) DO NOTHING;

-- Urban / rural tags sourced from PSA/CPH.
CREATE TABLE IF NOT EXISTS urban_rural_tags (
    tag_code TEXT PRIMARY KEY,
    description TEXT NOT NULL
);

INSERT INTO urban_rural_tags (tag_code, description) VALUES
    ('U', 'Urban'),
    ('R', 'Rural'),
    ('-', 'Unclassified / mixed')
ON CONFLICT (tag_code) DO NOTHING;

-- Canonical list of PSGC-coded locations.
CREATE TABLE IF NOT EXISTS locations (
    psgc_code CHAR(10) PRIMARY KEY,
    name TEXT NOT NULL,
    level_code TEXT NOT NULL REFERENCES geographic_levels(level_code),
    parent_psgc CHAR(10) REFERENCES locations(psgc_code) ON UPDATE CASCADE ON DELETE RESTRICT,
    correspondence_code TEXT,
    status TEXT,
    old_names TEXT,
    effective_date DATE,
    retired_date DATE,
    geom GEOMETRY(MultiPolygon, 4326)
);

ALTER TABLE IF EXISTS locations
    ALTER COLUMN old_names TYPE TEXT;

CREATE INDEX IF NOT EXISTS idx_locations_parent ON locations(parent_psgc);
CREATE INDEX IF NOT EXISTS idx_locations_geom ON locations USING GIST (geom);

-- Population measurements (per reference year and source).
CREATE TABLE IF NOT EXISTS population_stats (
    population_id BIGSERIAL PRIMARY KEY,
    psgc_code CHAR(10) NOT NULL REFERENCES locations(psgc_code) ON DELETE CASCADE,
    reference_year SMALLINT NOT NULL,
    population BIGINT NOT NULL CHECK (population >= 0),
    source TEXT NOT NULL,
    collected_at DATE DEFAULT CURRENT_DATE,
    UNIQUE (psgc_code, reference_year, source)
);

CREATE INDEX IF NOT EXISTS idx_population_stats_year ON population_stats(reference_year);

-- City class assignments.
CREATE TABLE IF NOT EXISTS city_classifications (
    psgc_code CHAR(10) PRIMARY KEY REFERENCES locations(psgc_code) ON DELETE CASCADE,
    class_code TEXT NOT NULL REFERENCES city_class_types(class_code),
    source TEXT,
    effective_year SMALLINT
);

-- Income class assignments (municipalities/cities/provinces).
CREATE TABLE IF NOT EXISTS income_classifications (
    psgc_code CHAR(10) PRIMARY KEY REFERENCES locations(psgc_code) ON DELETE CASCADE,
    bracket_code TEXT NOT NULL REFERENCES income_brackets(bracket_code),
    source TEXT,
    effective_year SMALLINT
);

-- Urban / rural tagging (typically barangay level).
CREATE TABLE IF NOT EXISTS settlement_tags (
    psgc_code CHAR(10) PRIMARY KEY REFERENCES locations(psgc_code) ON DELETE CASCADE,
    tag_code TEXT NOT NULL REFERENCES urban_rural_tags(tag_code),
    source TEXT,
    reference_year SMALLINT
);

-- Optional table for PSA notes / release metadata.
CREATE TABLE IF NOT EXISTS release_notes (
    note_id SERIAL PRIMARY KEY,
    release_id TEXT NOT NULL,
    sheet_name TEXT,
    note TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
