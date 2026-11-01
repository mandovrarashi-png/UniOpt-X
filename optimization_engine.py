
from collections import defaultdict


class SchedulingError(Exception):
    pass


def generate_schedule(cursor, node_limit=200000):
    """
    Constraint-based timetable generator using backtracking
    and a scheduling preference score.

    Returns a complete list of timetable rows.
    Does not commit or modify the database.
    """

    def fetch_all(sql):
        cursor.execute(sql)
        columns = [d[0].lower() for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    # ------------------------------------------------
    # 1. LOAD DATABASE DATA
    # ------------------------------------------------

    slots = fetch_all("""
        SELECT slot_id, day_no, period_no
        FROM time_slot
        ORDER BY day_no, period_no
    """)

    classes = fetch_all("""
        SELECT class_id, class_name, student_strength
        FROM classes
    """)

    faculty = fetch_all("""
        SELECT faculty_id, faculty_name, max_periods_per_week
        FROM faculty
    """)

    subjects = fetch_all("""
        SELECT subject_id, subject_name, is_lab
        FROM subject
    """)

    rooms = fetch_all("""
        SELECT room_id, room_name, is_lab, capacity
        FROM room
    """)

    assignments = fetch_all("""
        SELECT class_id, subject_id, faculty_id, periods_per_week
        FROM class_subject
    """)

    unavailable = fetch_all("""
        SELECT faculty_id, slot_id
        FROM faculty_availability
        WHERE is_available = 0
    """)

    # ------------------------------------------------
    # 2. VALIDATE REQUIRED DATA
    # ------------------------------------------------

    if not slots:
        raise SchedulingError("Create weekly time slots first.")

    if not classes:
        raise SchedulingError("Add sections first.")

    if not faculty:
        raise SchedulingError("Add faculty first.")

    if not subjects:
        raise SchedulingError("Add subjects first.")

    if not rooms:
        raise SchedulingError("Add rooms first.")

    if not assignments:
        raise SchedulingError("Assign subjects to sections first.")

    slot_map = {s["slot_id"]: s for s in slots}
    class_map = {c["class_id"]: c for c in classes}
    faculty_map = {f["faculty_id"]: f for f in faculty}
    subject_map = {s["subject_id"]: s for s in subjects}

    unavailable_set = {
        (x["faculty_id"], x["slot_id"])
        for x in unavailable
    }

    # ------------------------------------------------
    # 3. FIND CONSECUTIVE SLOTS
    # ------------------------------------------------

    next_slot = {}

    slots_by_day = defaultdict(dict)

    for slot in slots:
        slots_by_day[slot["day_no"]][slot["period_no"]] = slot["slot_id"]

    for day, periods in slots_by_day.items():
        for period, slot_id in periods.items():

            # Period 3 is 12-1 PM.
            # Period 4 is 2-3 PM.
            # They are NOT consecutive because of lunch.
            if period == 3:
                continue

            next_id = periods.get(period + 1)

            if next_id is not None:
                next_slot[slot_id] = next_id

    # ------------------------------------------------
    # 4. VALIDATE ASSIGNMENTS AND BUILD UNITS
    # ------------------------------------------------

    units = []
    assignment_lookup = {}

    faculty_load = defaultdict(int)

    for a in assignments:

        cid = a["class_id"]
        sid = a["subject_id"]
        fid = a["faculty_id"]
        periods = a["periods_per_week"]

        if cid not in class_map:
            raise SchedulingError("Assignment contains unknown section.")

        if sid not in subject_map:
            raise SchedulingError("Assignment contains unknown subject.")

        if fid not in faculty_map:
            raise SchedulingError("Assignment contains unknown faculty.")

        if periods is None or periods <= 0:
            raise SchedulingError("Weekly periods must be positive.")

        subject = subject_map[sid]
        is_lab = subject["is_lab"] == 1

        block_size = 2 if is_lab else 1

        if is_lab and periods % 2 != 0:
            raise SchedulingError(
                f"Lab subject {subject['subject_name']} "
                "must have an even number of weekly periods."
            )

        faculty_load[fid] += periods

        assignment_lookup[(cid, sid, fid)] = a

        for occurrence in range(periods // block_size):
            units.append({
                "class_id": cid,
                "subject_id": sid,
                "faculty_id": fid,
                "block_size": block_size,
                "occurrence": occurrence
            })

    # ------------------------------------------------
    # 5. FACULTY MAXIMUM WORKLOAD
    # ------------------------------------------------

    for fid, load in faculty_load.items():

        maximum = faculty_map[fid]["max_periods_per_week"]

        if maximum is not None and load > maximum:
            raise SchedulingError(
                f"{faculty_map[fid]['faculty_name']} has "
                f"{load} assigned periods, exceeding maximum {maximum}."
            )

    # ------------------------------------------------
    # 6. FIND SUITABLE ROOMS
    # ------------------------------------------------

    room_candidates = {}

    for a in assignments:

        cid = a["class_id"]
        sid = a["subject_id"]

        section = class_map[cid]
        subject = subject_map[sid]

        if section["student_strength"] is None:
            raise SchedulingError(
                f"Student strength missing for {section['class_name']}."
            )

        suitable = [
            r for r in rooms
            if r["is_lab"] == subject["is_lab"]
            and r["capacity"] is not None
            and r["capacity"] >= section["student_strength"]
        ]

        if not suitable:
            raise SchedulingError(
                f"No suitable room for {subject['subject_name']} "
                f"in {section['class_name']}."
            )

        room_candidates[(cid, sid)] = suitable

    # ------------------------------------------------
    # 7. CONFLICT TRACKING
    # ------------------------------------------------

    class_busy = defaultdict(set)
    faculty_busy = defaultdict(set)
    room_busy = defaultdict(set)

    class_daily_load = defaultdict(int)
    faculty_daily_load = defaultdict(int)

    scheduled = []

    # ------------------------------------------------
    # 8. POSSIBLE PLACEMENTS
    # ------------------------------------------------

    def possible_placements(unit):

        cid = unit["class_id"]
        sid = unit["subject_id"]
        fid = unit["faculty_id"]
        block_size = unit["block_size"]

        result = []

        for first in slots:

            first_id = first["slot_id"]
            selected_slots = [first_id]

            if block_size == 2:

                second_id = next_slot.get(first_id)

                if second_id is None:
                    continue

                selected_slots.append(second_id)

            if any(x in class_busy[cid] for x in selected_slots):
                continue

            if any(x in faculty_busy[fid] for x in selected_slots):
                continue

            if any(
                (fid, x) in unavailable_set
                for x in selected_slots
            ):
                continue

            for room in room_candidates[(cid, sid)]:

                rid = room["room_id"]

                if any(x in room_busy[rid] for x in selected_slots):
                    continue

                day = first["day_no"]

                same_day_count = sum(
                    1 for row in scheduled
                    if row["class_id"] == cid
                    and row["subject_id"] == sid
                    and slot_map[row["slot_id"]]["day_no"] == day
                )

                section_load = class_daily_load[(cid, day)]
                teacher_load = faculty_daily_load[(fid, day)]

                # Lower score is preferred.
                score = (
                    same_day_count * 100
                    + section_load * 3
                    + teacher_load * 2
                )

                result.append({
                    "slot_ids": selected_slots,
                    "room_id": rid,
                    "score": score
                })

        result.sort(key=lambda x: x["score"])

        return result

    # ------------------------------------------------
    # 9. BACKTRACKING SCHEDULER
    # ------------------------------------------------

    # Schedule labs first because they have stricter requirements.
    units.sort(
        key=lambda u: (
            -u["block_size"],
            u["class_id"],
            u["subject_id"],
            u["occurrence"]
        )
    )

    nodes = 0

    def search(index):

        nonlocal nodes

        nodes += 1

        if nodes > node_limit:
            raise SchedulingError(
                "Search limit reached. Review constraints or "
                "reduce the scheduling complexity."
            )

        if index == len(units):
            return True

        unit = units[index]

        cid = unit["class_id"]
        sid = unit["subject_id"]
        fid = unit["faculty_id"]

        candidates = possible_placements(unit)

        if not candidates:
            return False

        for candidate in candidates:

            selected_slots = candidate["slot_ids"]
            rid = candidate["room_id"]

            # Apply placement.
            for slot_id in selected_slots:

                day = slot_map[slot_id]["day_no"]

                class_busy[cid].add(slot_id)
                faculty_busy[fid].add(slot_id)
                room_busy[rid].add(slot_id)

                class_daily_load[(cid, day)] += 1
                faculty_daily_load[(fid, day)] += 1

                scheduled.append({
                    "class_id": cid,
                    "subject_id": sid,
                    "faculty_id": fid,
                    "room_id": rid,
                    "slot_id": slot_id
                })

            if search(index + 1):
                return True

            # Undo placement.
            for slot_id in selected_slots:

                day = slot_map[slot_id]["day_no"]

                class_busy[cid].remove(slot_id)
                faculty_busy[fid].remove(slot_id)
                room_busy[rid].remove(slot_id)

                class_daily_load[(cid, day)] -= 1
                faculty_daily_load[(fid, day)] -= 1

                scheduled.pop()

        return False

    if not search(0):
        raise SchedulingError(
            "No valid timetable found. Check faculty availability, "
            "room capacity, weekly periods and available slots."
        )

    # ------------------------------------------------
    # 10. FINAL VERIFICATION
    # ------------------------------------------------

    expected_periods = sum(
        a["periods_per_week"] for a in assignments
    )

    if len(scheduled) != expected_periods:
        raise SchedulingError(
            "Final verification failed. Some periods are missing."
        )

    return scheduled
