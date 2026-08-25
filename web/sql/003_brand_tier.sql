-- Two judgements the listing text supports but never states plainly:
-- how good is the brand, and what is actually in the box.
--
-- Both exist because the same $5 is a different bet depending on the answer.
-- A Dreame L40s Ultra and a ZCWA D15S MAX are both "robot vacuum, $5,
-- untested"; one has parts, firmware and a service path and the other is
-- landfill the day its pump dies. And "SEALED" versus "Power: Untested" is the
-- difference between buying stock and buying a lottery ticket — a distinction
-- that took reading forty descriptions by hand to make once.
--
-- Deliberately in `web` rather than a dbt seed beside reference.wishlist. The
-- wishlist decides what is *shown*, which is pipeline truth; this decides how
-- it is *judged*, which is opinion and gets revised whenever a brand
-- disappoints. It can be edited with an UPDATE and re-read on the next Rescan,
-- with no dbt run. If it settles, promote it.


-- ---------------------------------------------------------------------------
-- brand_tier
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS web.brand_tier (
    brand    TEXT PRIMARY KEY,
    pattern  TEXT NOT NULL,

    -- premium    — parts, firmware and a service path years from now
    -- solid      — good hardware, thinner support
    -- budget     — works, but treat as disposable
    -- whitelabel — Amazon-brand, no parts, no service, no firmware
    tier     TEXT NOT NULL CHECK (tier IN ('premium', 'solid', 'budget', 'whitelabel')),

    -- Lower wins when several patterns match. Needed because model names are
    -- more specific than brand names and both appear: "SEALED ECOVACS
    -- R-OZX1PLUS DEEBOT X1 PLUS" matches both `ecovacs` and `deebot`, and a
    -- listing can name a component brand ("Brand: Intel") that is not the
    -- brand of the thing being sold.
    priority INTEGER NOT NULL DEFAULT 100,
    note     TEXT
);

COMMENT ON TABLE web.brand_tier IS
    'How much a brand is worth trusting at liquidation prices. Opinion, not '
    'pipeline truth — edit freely, re-read on the next Rescan.';

INSERT INTO web.brand_tier (brand, pattern, tier, priority, note) VALUES
    -- Robot vacuums. The tier gap here is the widest in the corpus.
    ('roborock',    '\mroborock\M|\msaros\M|\mqrevo\M',        'premium', 50,
        'Best-supported robot vac line; parts and firmware for years.'),
    -- Model lines matter as much as brand names here: the salvage auction
    -- titles its lots "L60 Ultra FE Robot Vacuum" with no maker anywhere in
    -- the title, and those are Dreame flagships selling beside white-label
    -- tat at the same $5.
    ('Dreame',      '\mdreame\M|\ml[1-6]0s?\M ?(ultra|pro)|\ml10s\M|\ml40s\M|\ml60\M',
                                                                'premium', 50,
        'Flagship-grade hardware, real parts channel.'),
    ('Ecovacs',     '\mecovacs\M|\mdeebot\M|\mwinbot\M',       'solid',   60,
        'Good machines; support thinner than roborock/Dreame.'),
    ('Narwal',      '\mnarwal\M|\mfreo\M|\mflow\M robot',           'solid',   60,
        'Excellent mopping, small company — parts risk.'),
    ('iRobot',      '\mirobot\M|\mroomba\M|\mbraava\M',        'solid',   60,
        'Ubiquitous parts; mopping is weak outside Braava.'),
    ('Shark',       '\mshark\M',                               'solid',   70, NULL),
    ('Eufy',        '\meufy\M|\manker\M',                      'solid',   70, NULL),
    ('Eureka',      '\meureka\M',                              'budget',  80, NULL),
    ('Bissell',     '\mbissell\M',                             'budget',  80, NULL),
    ('Ultenic',     '\multenic\M',                             'budget',  80, NULL),
    ('ZCWA',        '\mzcwa\M|\md15s\M',                       'whitelabel', 40,
        'Amazon white-label. No parts, no service. Untested = landfill.'),
    ('Redroad',     '\mredroad\M',                             'whitelabel', 40, NULL),
    ('Tikom',       '\mtikom\M',                               'whitelabel', 40, NULL),
    ('Lefant',      '\mlefant\M',                              'whitelabel', 40, NULL),
    ('Yeedi',       '\myeedi\M',                               'budget',  80, NULL),

    -- Air quality / purifiers
    ('Airthings',   '\mairthings\M|corentium',                 'premium', 50, NULL),
    ('Dyson',       '\mdyson\M',                               'premium', 50, NULL),
    ('Blueair',     '\mblueair\M',                             'solid',   60, NULL),
    ('Coway',       '\mcoway\M|\mairmega\M',                    'solid',   60, NULL),
    ('Levoit',      '\mlevoit\M',                              'budget',  80,
        'Cloud-only via VeSync; fine hardware, rented integration.'),
    ('Xiaomi',      '\mxiaomi\M|\msmartmi\M',                   'solid',   70, NULL),
    ('IKEA',        '\mvindstyrka\M|\mvindriktning\M',          'budget',  60, NULL),

    -- Networking
    ('Ubiquiti',    '\mubiquiti\M|\munifi\M|\medgerouter\M',    'premium', 50, NULL),
    ('Synology',    '\msynology\M',                            'premium', 50, NULL),
    ('TP-Link',     '\mtp-?link\M|\mdeco\M|\mtapo\M',           'solid',   70, NULL),
    ('Netgear',     '\mnetgear\M|\morbi\M',                     'solid',   70, NULL),

    -- Storage
    ('Samsung',     '\msamsung\M',                             'premium', 50, NULL),
    ('Western Digital', '\mwestern digital\M|\mwd\M|\mwd_black\M|sandisk', 'premium', 55, NULL),
    ('Seagate',     '\mseagate\M|\mironwolf\M|\mexos\M|barracuda', 'solid', 60, NULL),
    ('Crucial',     '\mcrucial\M|\mmicron\M',                   'solid',   60, NULL),
    ('Kingston',    '\mkingston\M',                            'solid',   70, NULL),

    -- Computing
    ('Apple',       '\mapple\M|\mipad\M|\mmacbook\M|\mmac ?mini\M', 'premium', 50, NULL),
    ('Lenovo',      '\mlenovo\M|thinkcentre|thinkpad|thinkstation', 'premium', 55, NULL),
    ('HP',          '\melitedesk\M|\mprodesk\M|\melitebook\M',  'premium', 55, NULL),
    ('Dell',        '\moptiplex\M|\mlatitude\M|\mprecision\M',  'premium', 55, NULL),
    ('Intel NUC',   '\mnuc\M',                                 'premium', 45, NULL),
    ('ASUS',        '\masus\M',                                'solid',   70, NULL),
    ('Beelink',     '\mbeelink\M|\mminisforum\M',               'budget',  80, NULL),

    -- Audio / display / readers
    ('Sony',        '\msony\M|wh-?1000|\mxm[3-7]\M',            'premium', 50, NULL),
    ('Bose',        '\mbose\M|quietcomfort',                    'premium', 50, NULL),
    ('Sennheiser',  '\msennheiser\M|\mmomentum\M',              'premium', 50, NULL),
    ('LG',          '\mlg\M',                                  'premium', 60, NULL),
    ('Kobo',        '\mkobo\M',                                'solid',   60, NULL),
    ('Kindle',      '\mkindle\M|paperwhite',                    'solid',   60, NULL),
    ('Boox',        '\mboox\M|\mremarkable\M|supernote',        'solid',   60, NULL),

    -- Home automation
    ('Philips Hue', 'philips hue|\mhue\M',                      'premium', 50, NULL),
    ('Lutron',      '\mlutron\M|caseta',                        'premium', 50, NULL),
    ('Inovelli',    '\minovelli\M|\mzooz\M',                    'premium', 50, NULL),
    ('Aqara',       '\maqara\M|switchbot',                      'solid',   70, NULL),
    ('Wyze',        '\mwyze\M',                                'budget',  80, NULL),

    -- Power / tools
    ('APC',         '\mapc\M|smart-?ups',                       'premium', 55, NULL),
    ('CyberPower',  '\mcyberpower\M',                          'solid',   65, NULL),
    ('Fluke',       '\mfluke\M',                               'premium', 45, NULL),
    ('Belimo',      '\mbelimo\M|honeywell',                     'solid',   60, NULL),
    -- Bicycles. The tier gap is real money here: a Trek or Kona frame is worth
    -- rebuilding, a department-store bike is heavy at any price.
    ('Trek',        '\mtrek\M|gary fisher',                    'premium', 50, NULL),
    ('Kona',        '\mkona\M',                                'premium', 50, NULL),
    ('Specialized', '\mspecialized\M|crosstrail',              'premium', 50, NULL),
    ('Cannondale',  '\mcannondale\M',                          'premium', 50, NULL),
    ('Giant',       '\mgiant\M',                               'premium', 55, NULL),
    ('Marin',       '\mmarin\M',                               'solid',   60, NULL),
    ('Norco',       '\mnorco\M',                               'solid',   60, NULL),
    ('Decathlon',   '\mdecathlon\M|\mtriban\M',               'solid',   60,
        'Own-brand but properly engineered; parts are standard.'),
    ('Diamondback', '\mdiamondback\M|\mgt\M',                 'budget',  70, NULL),
    ('Huffy',       '\mhuffy\M|supercycle|roadmaster|\mkent\M', 'whitelabel', 40,
        'Department-store bike. Heavy at any price.'),

    -- E-bikes
    ('IGO',         '\migo\M',                                 'solid',   60,
        'Canadian e-bike brand with a dealer network — batteries obtainable.'),
    ('Jetson',      '\mjetson\M',                              'budget',  75,
        'Big-box e-bike. Proprietary battery, thin parts channel.'),
    ('Damco',       '\mdamco\M',                               'budget',  80, NULL),
    ('Concorde',    '\mconcorde\M',                            'solid',   70,
        'Vintage road marque. Steel frames run large, which suits a tall rider.'),

    -- Smart switches / home
    ('Treatlife',   '\mtreatlife\M',                           'budget',  80,
        'Cloud-only Tuya switch. Works, but a rented integration.'),
    ('Amazon Basics','amazon ?basics',                          'whitelabel', 40, NULL),

    -- Storage
    ('Yotuo',       '\myotuo\M',                               'whitelabel', 40, NULL),

    -- Tools / outdoor
    ('John Deere',  'john deere',                               'premium', 50, NULL),
    ('ECCPP',       '\meccpp\M',                               'whitelabel', 40, NULL),
    ('Milwaukee',   '\mmilwaukee\M|\mfuel m1[28]\M',           'premium', 50, NULL),
    ('DeWalt',      '\mdewalt\M',                              'premium', 50, NULL),
    ('Makita',      '\mmakita\M',                              'premium', 50, NULL),
    ('Ryobi',       '\mryobi\M',                               'solid',   65, NULL),

    -- The absence of a brand, named.
    --
    -- Not a gap in this table — a finding. Eleven live lots are titled exactly
    -- "SSD HardDisk" or "External SSD 64 GB": no maker, no model, and a
    -- capacity that is the entire product description. That is the signature
    -- the storage_drives wishlist row already warns about ("the 64TB Hard
    -- disik listings are counterfeit"), and a controller reporting a capacity
    -- the flash cannot hold is the single most common fraud in this corpus.
    -- Rendering these blank hid the most important thing about them, so they
    -- get a name and the loudest tier.
    ('Unbranded drive',
     '^ *(new |sealed |open box )*(external |portable |usb )*(ssd|hdd|hard ?disk|hard ?drive)( ?(harddisk|hard ?disk|drive))?( +[0-9]+ ?(gb|tb))? *$',
                                                                'whitelabel', 20,
        'No maker named at all. Bare-capacity drive titles are the counterfeit '
        'pattern — verify actual written capacity before trusting the label.')
ON CONFLICT (brand) DO UPDATE SET
    pattern  = EXCLUDED.pattern,
    tier     = EXCLUDED.tier,
    priority = EXCLUDED.priority,
    note     = EXCLUDED.note;


-- The best-scoring brand for a piece of listing text, or no row.
--
-- RETURNS TABLE rather than a composite so a miss is zero rows and callers
-- use LEFT JOIN LATERAL — a composite would hand back a row of NULLs and hide
-- the difference between "no brand recognised" and "brand with no note".
CREATE OR REPLACE FUNCTION web.brand_of(p_text TEXT)
RETURNS TABLE (brand TEXT, tier TEXT, note TEXT) AS $$
    SELECT b.brand, b.tier, b.note
    FROM web.brand_tier b
    WHERE p_text ~* b.pattern
    ORDER BY b.priority, length(b.pattern) DESC
    LIMIT 1
$$ LANGUAGE sql STABLE;


-- ---------------------------------------------------------------------------
-- condition_grade
-- ---------------------------------------------------------------------------
--
-- What is actually in the box, read out of whatever the seller happened to
-- write. Every auction house uses its own vocabulary, so this is a ladder of
-- patterns rather than a lookup.
--
-- ORDER IS THE WHOLE DESIGN: the disqualifying signals are tested first, so a
-- pallet listing that says both "SEALED" and "Power: Untested" grades as
-- untested. Checking the flattering words first would let every salvage lot
-- in the corpus claim to be sealed stock, which is exactly the mistake this
-- exists to prevent.
CREATE OR REPLACE FUNCTION web.condition_grade(p_title TEXT, p_descr TEXT)
RETURNS TEXT AS $$
    SELECT CASE
        WHEN t ~* 'power: not working|\mnot working\M|\mfor parts\M|\mparts only\M' THEN 'broken'
        WHEN t ~* '\mas-?is\M'                                THEN 'asis'
        WHEN t ~* 'power: untested|\muntested\M'              THEN 'untested'
        WHEN t ~* '\msealed\M'                                THEN 'sealed'
        WHEN t ~* 'new open box|\mnob\M|\mopen box\M|condition: new' THEN 'new'
        WHEN t ~* 'functional\?: yes'                         THEN 'tested'
        WHEN t ~* 'condition: (very good|like new|excellent)' THEN 'good'
        WHEN t ~* 'sign of handling|condition: used|\mused\M|\mrenewed\M|refurb' THEN 'used'
        ELSE 'unknown'
    END
    FROM (SELECT coalesce(p_title,'') || ' ' || coalesce(p_descr,'') AS t) s
$$ LANGUAGE sql IMMUTABLE;


-- Sort key for the ladder above. Best box first, which is the order someone
-- buying sealed stock wants to read the list in.
CREATE OR REPLACE FUNCTION web.condition_rank(p_grade TEXT)
RETURNS INTEGER AS $$
    SELECT CASE p_grade
        WHEN 'sealed'   THEN 1
        WHEN 'new'      THEN 2
        WHEN 'tested'   THEN 3
        WHEN 'good'     THEN 4
        WHEN 'unknown'  THEN 5
        WHEN 'used'     THEN 6
        WHEN 'untested' THEN 7
        WHEN 'broken'   THEN 8
        WHEN 'asis'     THEN 9
        ELSE 10
    END
$$ LANGUAGE sql IMMUTABLE;


-- Carried on the match cache so the list can filter and sort on them without
-- re-reading descriptions. Populated by the rebuild in lots.py.
ALTER TABLE web.wishlist_match ADD COLUMN IF NOT EXISTS brand           TEXT;
ALTER TABLE web.wishlist_match ADD COLUMN IF NOT EXISTS brand_tier      TEXT;
ALTER TABLE web.wishlist_match ADD COLUMN IF NOT EXISTS condition_grade TEXT;


-- Brand from the title, falling back to the description.
--
-- Title first because descriptions name component makers, not the maker of the
-- thing being sold — a used laptop in this corpus carries "Brand: Intel Model:
-- N5100", which is true of its CPU and nothing else. But title-only left a
-- third of the robot vacuums unattributed, because the salvage auction lists
-- them as bare model names, so the description is consulted only when the
-- title yields nothing at all.
CREATE OR REPLACE FUNCTION web.brand_for(p_title TEXT, p_descr TEXT)
RETURNS TABLE (brand TEXT, tier TEXT, note TEXT) AS $$
    SELECT * FROM web.brand_of(p_title)
    UNION ALL
    SELECT * FROM web.brand_of(p_descr)
     WHERE NOT EXISTS (SELECT 1 FROM web.brand_of(p_title))
    LIMIT 1
$$ LANGUAGE sql STABLE;
