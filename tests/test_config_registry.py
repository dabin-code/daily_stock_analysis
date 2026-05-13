from src.core.config_registry import build_schema_response, get_field_definition


def test_feishu_stream_fields_are_explicitly_registered():
    stream_field = get_field_definition("FEISHU_STREAM_ENABLED")
    assert stream_field["category"] == "notification"
    assert stream_field["data_type"] == "boolean"
    assert stream_field["ui_control"] == "switch"
    assert stream_field["default_value"] == "false"

    verification_field = get_field_definition("FEISHU_VERIFICATION_TOKEN")
    assert verification_field["category"] == "notification"
    assert verification_field["ui_control"] == "password"
    assert verification_field["is_sensitive"] is True

    encrypt_field = get_field_definition("FEISHU_ENCRYPT_KEY")
    assert encrypt_field["category"] == "notification"
    assert encrypt_field["ui_control"] == "password"
    assert encrypt_field["is_sensitive"] is True


def test_feishu_stream_fields_appear_in_schema_response():
    schema = build_schema_response()
    notification_category = next(
        category for category in schema["categories"] if category["category"] == "notification"
    )
    keys = {field["key"] for field in notification_category["fields"]}

    assert "FEISHU_STREAM_ENABLED" in keys
    assert "FEISHU_VERIFICATION_TOKEN" in keys
    assert "FEISHU_ENCRYPT_KEY" in keys


def test_screening_fields_are_explicitly_registered():
    default_mode = get_field_definition("SCREENING_DEFAULT_MODE")
    assert default_mode["category"] == "screening"
    assert default_mode["data_type"] == "string"
    assert default_mode["ui_control"] == "select"
    assert default_mode["default_value"] == "balanced"

    candidate_limit = get_field_definition("SCREENING_CANDIDATE_LIMIT")
    assert candidate_limit["category"] == "screening"
    assert candidate_limit["data_type"] == "integer"
    assert candidate_limit["ui_control"] == "number"
    assert candidate_limit["default_value"] == "30"

    ai_top_k = get_field_definition("SCREENING_AI_TOP_K")
    assert ai_top_k["category"] == "screening"
    assert ai_top_k["data_type"] == "integer"
    assert ai_top_k["default_value"] == "10"

    schedule_enabled = get_field_definition("SCREENING_SCHEDULE_ENABLED")
    assert schedule_enabled["category"] == "screening"
    assert schedule_enabled["data_type"] == "boolean"
    assert schedule_enabled["default_value"] == "false"

    schedule_time = get_field_definition("SCREENING_SCHEDULE_TIME")
    assert schedule_time["category"] == "screening"
    assert schedule_time["data_type"] == "time"
    assert schedule_time["default_value"] == "07:00"

    schedule_run_immediately = get_field_definition("SCREENING_SCHEDULE_RUN_IMMEDIATELY")
    assert schedule_run_immediately["category"] == "screening"
    assert schedule_run_immediately["data_type"] == "boolean"
    assert schedule_run_immediately["default_value"] == "false"

    min_volume_ratio = get_field_definition("SCREENING_MIN_VOLUME_RATIO")
    assert min_volume_ratio["category"] == "screening"
    assert min_volume_ratio["data_type"] == "number"
    assert min_volume_ratio["ui_control"] == "number"


def test_screening_fields_appear_in_schema_response():
    schema = build_schema_response()
    screening_category = next(
        category for category in schema["categories"] if category["category"] == "screening"
    )
    keys = {field["key"] for field in screening_category["fields"]}

    assert "SCREENING_DEFAULT_MODE" in keys
    assert "SCREENING_CANDIDATE_LIMIT" in keys
    assert "SCREENING_AI_TOP_K" in keys
    assert "SCREENING_SCHEDULE_ENABLED" in keys
    assert "SCREENING_SCHEDULE_TIME" in keys
    assert "SCREENING_SCHEDULE_RUN_IMMEDIATELY" in keys
    assert "SCREENING_MIN_LIST_DAYS" in keys
    assert "SCREENING_MIN_VOLUME_RATIO" in keys
    assert "SCREENING_MIN_AVG_AMOUNT" not in keys
    assert "SCREENING_BREAKOUT_LOOKBACK_DAYS" in keys
    assert "SCREENING_FACTOR_LOOKBACK_DAYS" in keys
