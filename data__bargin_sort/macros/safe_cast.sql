{#
    Numeric casts that tolerate HiBid's free text.

    Fields that look numeric are not reliably so — `bidQuantity` carries values
    like " x 2" meaning "two units per bid". A plain ::int on those aborts the
    whole model, so anything that is not cleanly a number becomes NULL and the
    original string is kept alongside in its own column.

    NULL here means "not a number", never zero.
#}

{% macro safe_int(expr) -%}
    case
        when ({{ expr }}) ~ '^\s*-?[0-9]+\s*$'
        then trim({{ expr }})::bigint
    end
{%- endmacro %}


{% macro safe_numeric(expr) -%}
    case
        when ({{ expr }}) ~ '^\s*-?[0-9]+(\.[0-9]+)?\s*$'
        then trim({{ expr }})::numeric
    end
{%- endmacro %}
