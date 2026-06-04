import sqlite3
conn = sqlite3.connect('/app/db/store_intelligence.db')

entry = conn.execute('SELECT COUNT(DISTINCT visitor_id) FROM events WHERE store_id="ST1008" AND event_type="ENTRY" AND is_staff=0').fetchone()[0]
zone = conn.execute('SELECT COUNT(DISTINCT visitor_id) FROM events WHERE store_id="ST1008" AND event_type="ZONE_ENTER" AND is_staff=0').fetchone()[0]
all_vis = conn.execute('SELECT COUNT(DISTINCT visitor_id) FROM events WHERE store_id="ST1008" AND is_staff=0').fetchone()[0]
zones = conn.execute('SELECT zone_id, COUNT(DISTINCT visitor_id), COUNT(*) FROM events WHERE store_id="ST1008" AND is_staff=0 AND event_type="ZONE_DWELL" GROUP BY zone_id').fetchall()
billing = conn.execute('SELECT COUNT(DISTINCT visitor_id) FROM events WHERE store_id="ST1008" AND event_type="BILLING_QUEUE_JOIN" AND is_staff=0').fetchone()[0]

print('ENTRY visitors    :', entry)
print('ZONE_ENTER visitors:', zone)
print('All non-staff     :', all_vis)
print('BILLING visitors  :', billing)
print('Zone dwell detail :')
for z in zones:
    print(' ', z)
conn.close()
