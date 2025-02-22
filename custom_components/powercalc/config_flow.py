"""Config flow for Adaptive Lighting integration."""

from __future__ import annotations

import copy
import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry, OptionsFlow
from homeassistant.const import (
    CONF_ATTRIBUTE,
    CONF_ENTITY_ID,
    CONF_NAME,
    CONF_UNIQUE_ID,
    CONF_UNIT_OF_MEASUREMENT,
    ENERGY_KILO_WATT_HOUR,
    POWER_WATT,
    Platform,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.typing import DiscoveryInfoType

from .common import SourceEntity, create_source_entity
from .const import (
    CONF_CALCULATION_ENABLED_CONDITION,
    CONF_CALIBRATE,
    CONF_CREATE_ENERGY_SENSOR,
    CONF_CREATE_UTILITY_METERS,
    CONF_DAILY_FIXED_ENERGY,
    CONF_ENERGY_INTEGRATION_METHOD,
    CONF_FIXED,
    CONF_GAMMA_CURVE,
    CONF_GROUP,
    CONF_GROUP_ENERGY_ENTITIES,
    CONF_GROUP_MEMBER_SENSORS,
    CONF_GROUP_POWER_ENTITIES,
    CONF_HIDE_MEMBERS,
    CONF_IGNORE_UNAVAILABLE_STATE,
    CONF_LINEAR,
    CONF_MANUFACTURER,
    CONF_MAX_POWER,
    CONF_MIN_POWER,
    CONF_MODE,
    CONF_MODEL,
    CONF_MULTIPLY_FACTOR,
    CONF_ON_TIME,
    CONF_POWER,
    CONF_POWER_TEMPLATE,
    CONF_SENSOR_TYPE,
    CONF_STANDBY_POWER,
    CONF_STATES_POWER,
    CONF_SUB_GROUPS,
    CONF_SUB_PROFILE,
    CONF_UNAVAILABLE_POWER,
    CONF_UPDATE_FREQUENCY,
    CONF_VALUE,
    CONF_VALUE_TEMPLATE,
    CONF_WLED,
    DISCOVERY_POWER_PROFILE,
    DISCOVERY_SOURCE_ENTITY,
    DOMAIN,
    ENERGY_INTEGRATION_METHOD_LEFT,
    ENERGY_INTEGRATION_METHODS,
    CalculationStrategy,
    SensorType,
)
from .discovery import autodiscover_model
from .errors import ModelNotSupported, StrategyConfigurationError
from .power_profile.factory import get_power_profile
from .power_profile.library import ModelInfo, ProfileLibrary
from .power_profile.power_profile import DEVICE_DOMAINS, PowerProfile
from .sensors.daily_energy import DEFAULT_DAILY_UPDATE_FREQUENCY
from .strategy.factory import PowerCalculatorStrategyFactory
from .strategy.strategy_interface import PowerCalculationStrategyInterface
from .strategy.wled import CONFIG_SCHEMA as SCHEMA_POWER_WLED

_LOGGER = logging.getLogger(__name__)

CONF_CONFIRM_AUTODISCOVERED_MODEL = "confirm_autodisovered_model"

MENU_OPTION_LIBRARY = "menu_library"

SENSOR_TYPE_MENU = {
    SensorType.DAILY_ENERGY: "Daily energy",
    SensorType.GROUP: "Group",
    SensorType.VIRTUAL_POWER: "Virtual power (manual)",
    MENU_OPTION_LIBRARY: "Virtual power (library)",
}

SCHEMA_DAILY_ENERGY_OPTIONS = vol.Schema(
    {
        vol.Optional(CONF_VALUE): vol.Coerce(float),
        vol.Optional(CONF_VALUE_TEMPLATE): selector.TemplateSelector(),
        vol.Optional(CONF_UNIT_OF_MEASUREMENT, default=ENERGY_KILO_WATT_HOUR): vol.In(
            [ENERGY_KILO_WATT_HOUR, POWER_WATT]
        ),
        vol.Optional(CONF_ON_TIME): selector.DurationSelector(
            selector.DurationSelectorConfig(enable_day=False)
        ),
        vol.Optional(
            CONF_UPDATE_FREQUENCY, default=DEFAULT_DAILY_UPDATE_FREQUENCY
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=10, unit_of_measurement="sec", mode=selector.NumberSelectorMode.BOX
            )
        ),
    }
)
SCHEMA_DAILY_ENERGY = vol.Schema(
    {
        vol.Required(CONF_NAME): selector.TextSelector(),
        vol.Optional(CONF_UNIQUE_ID): selector.TextSelector(),
    }
).extend(SCHEMA_DAILY_ENERGY_OPTIONS.schema)

SCHEMA_POWER_LIBRARY = vol.Schema(
    {
        vol.Required(CONF_ENTITY_ID): selector.EntitySelector(),
        vol.Optional(CONF_NAME): selector.TextSelector(),
        vol.Optional(CONF_UNIQUE_ID): selector.TextSelector(),
    }
)

SCHEMA_POWER_OPTIONS = vol.Schema(
    {
        vol.Optional(CONF_STANDBY_POWER): vol.Coerce(float),
        vol.Optional(
            CONF_CREATE_ENERGY_SENSOR, default=True
        ): selector.BooleanSelector(),
        vol.Optional(
            CONF_CREATE_UTILITY_METERS, default=False
        ): selector.BooleanSelector(),
    }
)

SCHEMA_POWER_OPTIONS_LIBRARY = vol.Schema(
    {
        vol.Optional(
            CONF_CREATE_ENERGY_SENSOR, default=True
        ): selector.BooleanSelector(),
        vol.Optional(
            CONF_CREATE_UTILITY_METERS, default=False
        ): selector.BooleanSelector(),
    }
)

SCHEMA_POWER_BASE = vol.Schema(
    {
        vol.Optional(CONF_NAME): selector.TextSelector(),
        vol.Optional(CONF_UNIQUE_ID): selector.TextSelector(),
    }
)

STRATEGY_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[
            CalculationStrategy.FIXED,
            CalculationStrategy.LINEAR,
            CalculationStrategy.WLED,
            CalculationStrategy.LUT,
        ],
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
)

SCHEMA_POWER_FIXED = vol.Schema(
    {
        vol.Optional(CONF_POWER): vol.Coerce(float),
        vol.Optional(CONF_POWER_TEMPLATE): selector.TemplateSelector(),
        vol.Optional(CONF_STATES_POWER): selector.ObjectSelector(),
    }
)

SCHEMA_POWER_LINEAR = vol.Schema(
    {
        vol.Optional(CONF_MIN_POWER): vol.Coerce(float),
        vol.Optional(CONF_MAX_POWER): vol.Coerce(float),
        vol.Optional(CONF_GAMMA_CURVE): vol.Coerce(float),
        vol.Optional(CONF_CALIBRATE): selector.ObjectSelector(),
    }
)

SCHEMA_POWER_AUTODISCOVERED = vol.Schema(
    {vol.Optional(CONF_CONFIRM_AUTODISCOVERED_MODEL, default=True): bool}
)

SCHEMA_POWER_ADVANCED = vol.Schema(
    {
        vol.Optional(CONF_CALCULATION_ENABLED_CONDITION): selector.TemplateSelector(),
        vol.Optional(CONF_IGNORE_UNAVAILABLE_STATE): selector.BooleanSelector(),
        vol.Optional(CONF_UNAVAILABLE_POWER): vol.Coerce(float),
        vol.Optional(CONF_MULTIPLY_FACTOR): vol.Coerce(float),
        vol.Optional(
            CONF_ENERGY_INTEGRATION_METHOD, default=ENERGY_INTEGRATION_METHOD_LEFT
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=ENERGY_INTEGRATION_METHODS,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
    }
)

SCHEMA_GROUP = vol.Schema(
    {
        vol.Required(CONF_NAME): str,
        vol.Optional(CONF_UNIQUE_ID): selector.TextSelector(),
    }
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for PowerCalc."""

    VERSION = 1

    def __init__(self):
        """Initialize options flow."""
        self.sensor_config: dict[str, Any] = {}
        self.selected_sensor_type: str | None = None
        self.name: str | None = None
        self.source_entity: SourceEntity | None = None
        self.source_entity_id: str | None = None
        self.power_profile: PowerProfile | None = None
        self.skip_advanced_step: bool = False
        self.is_library_flow: bool = False

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return OptionsFlowHandler(config_entry)

    async def async_step_integration_discovery(
        self, discovery_info: DiscoveryInfoType
    ) -> FlowResult:
        """Handle integration discovery."""

        _LOGGER.debug("Starting discovery flow: %s", discovery_info)

        self.skip_advanced_step = (
            True  # We don't want to ask advanced option when discovered
        )

        self.selected_sensor_type = SensorType.VIRTUAL_POWER
        self.source_entity = discovery_info[DISCOVERY_SOURCE_ENTITY]
        del discovery_info[DISCOVERY_SOURCE_ENTITY]

        self.source_entity_id = self.source_entity.entity_id
        self.name = self.source_entity.name
        unique_id = f"pc_{self.source_entity.unique_id}"
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        if DISCOVERY_POWER_PROFILE in discovery_info:
            self.power_profile = discovery_info[DISCOVERY_POWER_PROFILE]
            del discovery_info[DISCOVERY_POWER_PROFILE]

        self.sensor_config = discovery_info.copy()

        self.context["title_placeholders"] = {
            "name": self.source_entity.name,
            "manufacturer": self.sensor_config.get(CONF_MANUFACTURER),
            "model": self.sensor_config.get(CONF_MODEL),
        }
        self.is_library_flow = True

        if discovery_info.get(CONF_MODE) == CalculationStrategy.WLED:
            return await self.async_step_wled()

        return await self.async_step_library()

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Handle the initial step."""

        return self.async_show_menu(step_id="user", menu_options=SENSOR_TYPE_MENU)

    async def async_step_menu_library(
        self, user_input: dict[str, str] = None
    ) -> FlowResult:
        """
        Handle the Virtual power (library) step.
        We forward to the virtual_power step, but without the strategy selector displayed
        """
        self.is_library_flow = True
        return await self.async_step_virtual_power(user_input)

    async def async_step_virtual_power(
        self, user_input: dict[str, str] = None
    ) -> FlowResult:
        if user_input is not None:
            self.source_entity_id = user_input[CONF_ENTITY_ID]
            self.source_entity = await create_source_entity(
                self.source_entity_id, self.hass
            )
            unique_id = user_input.get(CONF_UNIQUE_ID)
            if not unique_id:
                source_unique_id = self.source_entity.unique_id or self.source_entity_id
                unique_id = f"pc_{source_unique_id}"

            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

            self.name = user_input.get(CONF_NAME) or self.source_entity.name
            self.selected_sensor_type = SensorType.VIRTUAL_POWER
            self.sensor_config.update(user_input)

            if (
                user_input.get(CONF_MODE) == CalculationStrategy.LUT
                or self.is_library_flow
            ):
                return await self.async_step_library()

            if user_input.get(CONF_MODE) == CalculationStrategy.FIXED:
                return await self.async_step_fixed()

            if user_input.get(CONF_MODE) == CalculationStrategy.LINEAR:
                return await self.async_step_linear()

            if user_input.get(CONF_MODE) == CalculationStrategy.WLED:
                return await self.async_step_wled()

        return self.async_show_form(
            step_id="virtual_power",
            data_schema=_create_virtual_power_schema(self.hass, self.is_library_flow),
            errors={},
            last_step=False,
        )

    async def async_step_daily_energy(
        self, user_input: dict[str, str] = None
    ) -> FlowResult:
        errors = _validate_daily_energy_input(user_input)

        if user_input is not None and not errors:
            self.selected_sensor_type = SensorType.DAILY_ENERGY
            self.name = user_input.get(CONF_NAME)
            unique_id = user_input.get(CONF_UNIQUE_ID) or user_input.get(CONF_NAME)
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

            self.sensor_config.update(
                {CONF_DAILY_FIXED_ENERGY: _build_daily_energy_config(user_input)}
            )
            return self.create_config_entry()

        return self.async_show_form(
            step_id="daily_energy",
            data_schema=SCHEMA_DAILY_ENERGY,
            errors=errors,
        )

    async def async_step_group(self, user_input: dict[str, str] = None) -> FlowResult:
        self.selected_sensor_type = SensorType.GROUP
        errors = _validate_group_input(user_input)
        if user_input is not None:
            self.name = user_input.get(CONF_NAME)
            self.sensor_config.update(user_input)

            unique_id = user_input.get(CONF_UNIQUE_ID) or user_input.get(CONF_NAME)
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

            if not errors:
                return self.create_config_entry()

        group_schema = SCHEMA_GROUP.extend(
            _create_group_options_schema(self.hass).schema
        )
        return self.async_show_form(
            step_id="group",
            data_schema=group_schema,
            errors=errors,
        )

    async def async_step_fixed(self, user_input: dict[str, str] = None) -> FlowResult:
        errors = {}
        if user_input is not None:
            if user_input.get(CONF_POWER_TEMPLATE):
                user_input[CONF_POWER] = user_input.get(CONF_POWER_TEMPLATE)
            self.sensor_config.update({CONF_FIXED: user_input})
            errors = await self.validate_strategy_config()
            if not errors:
                return await self.async_step_power_advanced()

        return self.async_show_form(
            step_id="fixed",
            data_schema=SCHEMA_POWER_FIXED,
            errors=errors,
            last_step=False,
        )

    async def async_step_linear(self, user_input: dict[str, str] = None) -> FlowResult:
        errors = {}
        if user_input is not None:
            self.sensor_config.update({CONF_LINEAR: user_input})
            errors = await self.validate_strategy_config()
            if not errors:
                return await self.async_step_power_advanced()

        return self.async_show_form(
            step_id="linear",
            data_schema=_create_linear_schema(self.source_entity_id),
            errors=errors,
            last_step=False,
        )

    async def async_step_wled(self, user_input: dict[str, str] = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self.sensor_config.update({CONF_WLED: user_input})
            errors = await self.validate_strategy_config()
            if not errors:
                return await self.async_step_power_advanced()

        return self.async_show_form(
            step_id="wled",
            data_schema=SCHEMA_POWER_WLED,
            errors=errors,
            last_step=False,
        )

    async def async_step_library(self, user_input: dict[str, str] = None) -> FlowResult:
        """
        Try to autodiscover manufacturer/model first.
        Ask the user to confirm this or forward to manual library selection
        """
        if user_input is not None:
            if user_input.get(CONF_CONFIRM_AUTODISCOVERED_MODEL) and self.power_profile:
                self.sensor_config.update(
                    {
                        CONF_MANUFACTURER: self.power_profile.manufacturer,
                        CONF_MODEL: self.power_profile.model,
                    }
                )
                return await self.async_step_post_library(user_input)

            return await self.async_step_manufacturer()

        if self.source_entity.entity_entry and self.power_profile is None:
            try:
                self.power_profile = await get_power_profile(
                    self.hass,
                    {},
                    await autodiscover_model(
                        self.hass, self.source_entity.entity_entry
                    ),
                )
            except ModelNotSupported:
                self.power_profile = None
        if self.power_profile:
            remarks = self.power_profile.config_flow_discovery_remarks
            if remarks:
                remarks = "\n\n" + remarks
            return self.async_show_form(
                step_id="library",
                description_placeholders={
                    "remarks": remarks,
                    "manufacturer": self.power_profile.manufacturer,
                    "model": self.power_profile.model,
                },
                data_schema=SCHEMA_POWER_AUTODISCOVERED,
                errors={},
                last_step=False,
            )

        return await self.async_step_manufacturer()

    async def async_step_manufacturer(
        self, user_input: dict[str, str] = None
    ) -> FlowResult:
        """Ask the user to select the manufacturer"""
        if user_input is not None:
            self.sensor_config.update(
                {CONF_MANUFACTURER: user_input.get(CONF_MANUFACTURER)}
            )
            return await self.async_step_model()

        schema = _create_schema_manufacturer(self.hass, self.source_entity.domain)
        return self.async_show_form(
            step_id="manufacturer",
            data_schema=schema,
            errors={},
            last_step=False,
        )

    async def async_step_model(self, user_input: dict[str, str] = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self.sensor_config.update({CONF_MODEL: user_input.get(CONF_MODEL)})
            library = ProfileLibrary.factory(self.hass)
            profile = await library.get_profile(
                ModelInfo(
                    self.sensor_config.get(CONF_MANUFACTURER),
                    self.sensor_config.get(CONF_MODEL),
                )
            )
            self.power_profile = profile
            errors = await self.validate_strategy_config()
            if not errors:
                return await self.async_step_post_library()

        return self.async_show_form(
            step_id="model",
            data_schema=await _create_schema_model(
                self.hass,
                self.sensor_config.get(CONF_MANUFACTURER),
                self.source_entity,
            ),
            description_placeholders={
                "supported_models_link": "https://github.com/bramstroker/homeassistant-powercalc/blob/master/docs/supported_models.md"
            },
            errors=errors,
            last_step=False,
        )

    async def async_step_post_library(self, user_input: dict[str, str] = None):
        """Handles the logic after the user either selected manufacturer/model himself or confirmed autodiscovered"""
        if (
            self.power_profile.has_sub_profiles
            and not self.power_profile.sub_profile_select
        ):
            return await self.async_step_sub_profile()

        if self.power_profile.needs_fixed_config:
            return await self.async_step_fixed()

        return await self.async_step_power_advanced()

    async def async_step_sub_profile(
        self, user_input: dict[str, str] = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            # Append the sub profile to the model
            model = f"{self.sensor_config.get(CONF_MODEL)}/{user_input.get(CONF_SUB_PROFILE)}"
            self.sensor_config[CONF_MODEL] = model
            return await self.async_step_power_advanced()

        model_info = ModelInfo(
            self.sensor_config.get(CONF_MANUFACTURER),
            self.sensor_config.get(CONF_MODEL),
        )
        return self.async_show_form(
            step_id="sub_profile",
            data_schema=await _create_schema_sub_profile(self.hass, model_info),
            errors=errors,
            last_step=False,
        )

    async def async_step_power_advanced(
        self, user_input: dict[str, str] = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None or self.skip_advanced_step:
            self.sensor_config.update(user_input or {})
            return self.create_config_entry()

        return self.async_show_form(
            step_id="power_advanced",
            data_schema=SCHEMA_POWER_ADVANCED,
            errors=errors,
        )

    async def validate_strategy_config(self) -> dict:
        strategy_name = (
            self.sensor_config.get(CONF_MODE) or self.power_profile.calculation_strategy
        )
        strategy = await _create_strategy_object(
            self.hass,
            strategy_name,
            self.sensor_config,
            self.source_entity,
            self.power_profile,
        )
        try:
            await strategy.validate_config()
        except StrategyConfigurationError as error:
            translation = error.get_config_flow_translate_key()
            if translation is None:
                translation = "unknown"
            _LOGGER.error(str(error))
            return {"base": translation}
        return {}

    @callback
    def create_config_entry(self) -> FlowResult:
        if self.unique_id:
            self.sensor_config.update({CONF_UNIQUE_ID: self.unique_id})

        self.sensor_config.update({CONF_SENSOR_TYPE: self.selected_sensor_type})
        if self.name:
            self.sensor_config.update({CONF_NAME: self.name})
        if self.source_entity_id:
            self.sensor_config.update({CONF_ENTITY_ID: self.source_entity_id})

        return self.async_create_entry(title=self.name, data=self.sensor_config)


class OptionsFlowHandler(OptionsFlow):
    """Handle an option flow for PowerCalc."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry
        self.current_config: dict = dict(config_entry.data)
        self.sensor_type: SensorType = (
            self.current_config.get(CONF_SENSOR_TYPE) or SensorType.VIRTUAL_POWER
        )
        self.source_entity_id: str | None = self.current_config.get(CONF_ENTITY_ID)
        self.source_entity: SourceEntity | None = None
        self.power_profile: PowerProfile | None = None
        self.strategy: CalculationStrategy | None = self.current_config.get(CONF_MODE)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle options flow."""

        errors = {}
        self.current_config = dict(self.config_entry.data)
        if self.source_entity_id:
            self.source_entity = await create_source_entity(
                self.source_entity_id, self.hass
            )
            if self.current_config.get(CONF_MANUFACTURER) and self.current_config.get(
                CONF_MODEL
            ):
                try:
                    model_info = ModelInfo(
                        self.current_config.get(CONF_MANUFACTURER),
                        self.current_config.get(CONF_MODEL),
                    )
                    self.power_profile = await get_power_profile(
                        self.hass, {}, model_info
                    )
                    if self.power_profile and self.power_profile.needs_fixed_config:
                        self.strategy = CalculationStrategy.FIXED
                except ModelNotSupported:
                    errors["not_supported"] = "Power profile could not be loaded"

        if user_input is not None:
            errors = await self.save_options(user_input)
            if not errors:
                return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="init",
            data_schema=self.build_options_schema(),
            errors=errors,
        )

    async def save_options(self, user_input: dict[str, Any] | None = None) -> dict:
        """Save options, and return errors when validation fails"""
        if self.sensor_type == SensorType.DAILY_ENERGY:
            daily_energy_config = _build_daily_energy_config(user_input)
            self.current_config.update({CONF_DAILY_FIXED_ENERGY: daily_energy_config})

        if self.sensor_type == SensorType.VIRTUAL_POWER:
            generic_option_schema = SCHEMA_POWER_OPTIONS.extend(
                SCHEMA_POWER_ADVANCED.schema
            )
            generic_options = {}
            for key, val in generic_option_schema.schema.items():
                if isinstance(key, vol.Marker):
                    key = key.schema
                if key in user_input:
                    generic_options[key] = user_input.get(key)

            self.current_config.update(generic_options)

            if self.strategy:
                strategy_options = _build_strategy_config(
                    self.strategy, self.source_entity_id, user_input
                )

                if self.strategy != CalculationStrategy.LUT:
                    self.current_config.update({self.strategy: strategy_options})

                strategy_object = await _create_strategy_object(
                    self.hass, self.strategy, self.current_config, self.source_entity
                )
                try:
                    await strategy_object.validate_config()
                except StrategyConfigurationError as error:
                    return {"base": error.get_config_flow_translate_key()}

        if self.sensor_type == SensorType.GROUP:
            self.current_config.update(user_input)

        self.hass.config_entries.async_update_entry(
            self.config_entry, data=self.current_config
        )
        return {}

    def build_options_schema(self) -> vol.Schema:
        """Build the options schema. depending on the selected sensor type"""

        strategy_options = {}
        data_schema = {}
        if self.sensor_type == SensorType.VIRTUAL_POWER:
            if self.strategy:
                strategy_schema = _get_strategy_schema(
                    self.strategy, self.source_entity_id
                )
            else:
                strategy_schema = vol.Schema({})
            data_schema = SCHEMA_POWER_OPTIONS.extend(strategy_schema.schema).extend(
                SCHEMA_POWER_ADVANCED.schema
            )
            strategy_options = self.current_config.get(self.strategy) or {}

        if self.sensor_type == SensorType.DAILY_ENERGY:
            data_schema = SCHEMA_DAILY_ENERGY_OPTIONS
            strategy_options = self.current_config[CONF_DAILY_FIXED_ENERGY]

        if self.sensor_type == SensorType.GROUP:
            data_schema = _create_group_options_schema(self.hass)

        data_schema = _fill_schema_defaults(
            data_schema, self.current_config | strategy_options
        )
        return data_schema


async def _create_strategy_object(
    hass: HomeAssistant,
    strategy: str,
    config: dict,
    source_entity: SourceEntity,
    power_profile: PowerProfile | None = None,
) -> PowerCalculationStrategyInterface:
    """Create the calculation strategy object"""
    factory = PowerCalculatorStrategyFactory(hass)
    if power_profile is None and CONF_MANUFACTURER in config:
        power_profile = await ProfileLibrary.factory(hass).get_profile(
            ModelInfo(config.get(CONF_MANUFACTURER), config.get(CONF_MODEL))
        )
    return factory.create(config, strategy, power_profile, source_entity)


def _get_strategy_schema(strategy: str, source_entity_id: str) -> vol.Schema:
    """Get the config schema for a given power calculation strategy"""
    if strategy == CalculationStrategy.FIXED:
        return SCHEMA_POWER_FIXED
    if strategy == CalculationStrategy.LINEAR:
        return _create_linear_schema(source_entity_id)
    if strategy == CalculationStrategy.WLED:
        return SCHEMA_POWER_WLED
    if strategy == CalculationStrategy.LUT:
        return vol.Schema({})


def _create_virtual_power_schema(
    hass: HomeAssistant, is_library_flow: bool = True
) -> vol.Schema:
    if is_library_flow:
        entity_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(domain=list(DEVICE_DOMAINS.values()))
        )
    else:
        entity_selector = selector.EntitySelector()

    schema = vol.Schema(
        {
            vol.Required(CONF_ENTITY_ID): entity_selector,
        }
    ).extend(SCHEMA_POWER_BASE.schema)
    schema = schema.extend({vol.Optional(CONF_GROUP): _create_group_selector(hass)})
    if not is_library_flow:
        schema = schema.extend(
            {
                vol.Optional(
                    CONF_MODE, default=CalculationStrategy.FIXED
                ): STRATEGY_SELECTOR
            }
        )
        return schema.extend(SCHEMA_POWER_OPTIONS.schema)

    return schema.extend(SCHEMA_POWER_OPTIONS_LIBRARY.schema)


def _create_group_options_schema(hass: HomeAssistant) -> vol.Schema:
    """Create config schema for groups"""
    member_sensors = [
        selector.SelectOptionDict(value=config_entry.entry_id, label=config_entry.title)
        for config_entry in hass.config_entries.async_entries(DOMAIN)
        if config_entry.data.get(CONF_SENSOR_TYPE) == SensorType.VIRTUAL_POWER
        and config_entry.unique_id is not None
        and config_entry.title is not None
    ]
    member_sensor_selector = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=member_sensors,
            multiple=True,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )

    return vol.Schema(
        {
            vol.Optional(CONF_GROUP_MEMBER_SENSORS): member_sensor_selector,
            vol.Optional(CONF_GROUP_POWER_ENTITIES): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=Platform.SENSOR,
                    device_class=SensorDeviceClass.POWER,
                    multiple=True,
                )
            ),
            vol.Optional(CONF_GROUP_ENERGY_ENTITIES): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=Platform.SENSOR,
                    device_class=SensorDeviceClass.ENERGY,
                    multiple=True,
                )
            ),
            vol.Optional(CONF_SUB_GROUPS): _create_group_selector(hass, multiple=True),
            vol.Optional(
                CONF_CREATE_UTILITY_METERS, default=False
            ): selector.BooleanSelector(),
            vol.Optional(CONF_HIDE_MEMBERS, default=False): selector.BooleanSelector(),
        }
    )


def _create_group_selector(
    hass: HomeAssistant, multiple: bool = False
) -> selector.SelectSelector:
    options = [
        selector.SelectOptionDict(
            value=config_entry.entry_id, label=config_entry.data.get(CONF_NAME)
        )
        for config_entry in hass.config_entries.async_entries(DOMAIN)
        if config_entry.data.get(CONF_SENSOR_TYPE) == SensorType.GROUP
    ]

    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            multiple=multiple,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _validate_group_input(user_input: dict[str, str] = None) -> dict:
    """Validate the group form"""
    if not user_input:
        return {}
    errors: dict[str, str] = {}

    if (
        CONF_SUB_GROUPS not in user_input
        and CONF_GROUP_POWER_ENTITIES not in user_input
        and CONF_GROUP_ENERGY_ENTITIES not in user_input
        and CONF_GROUP_MEMBER_SENSORS not in user_input
    ):
        errors["base"] = "group_mandatory"

    return errors


def _create_linear_schema(source_entity_id: str) -> vol.Schema:
    """Create the config schema for linear strategy"""
    return SCHEMA_POWER_LINEAR.extend(
        {
            vol.Optional(CONF_ATTRIBUTE): selector.AttributeSelector(
                selector.AttributeSelectorConfig(entity_id=source_entity_id)
            )
        }
    )


def _create_schema_manufacturer(hass: HomeAssistant, entity_domain: str) -> vol.Schema:
    """Create manufacturer schema"""
    library = ProfileLibrary.factory(hass)
    manufacturers = [
        selector.SelectOptionDict(value=manufacturer, label=manufacturer)
        for manufacturer in library.get_manufacturer_listing(entity_domain)
    ]
    return vol.Schema(
        {
            vol.Required(CONF_MANUFACTURER): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=manufacturers, mode=selector.SelectSelectorMode.DROPDOWN
                )
            )
        }
    )


async def _create_schema_model(
    hass: HomeAssistant, manufacturer: str, source_entity: SourceEntity
) -> vol.Schema:
    """Create model schema"""
    library = ProfileLibrary.factory(hass)
    models = [
        selector.SelectOptionDict(value=profile.model, label=profile.model)
        for profile in await library.get_profiles_by_manufacturer(manufacturer)
        if profile.is_entity_domain_supported(source_entity)
    ]
    return vol.Schema(
        {
            vol.Required(CONF_MODEL): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=models, mode=selector.SelectSelectorMode.DROPDOWN
                )
            )
        }
    )


async def _create_schema_sub_profile(
    hass: HomeAssistant, model_info: ModelInfo
) -> vol.Schema:
    """Create sub profile schema"""
    library = ProfileLibrary.factory(hass)
    profile = await library.get_profile(model_info)
    sub_profiles = [
        selector.SelectOptionDict(value=sub_profile, label=sub_profile)
        for sub_profile in profile.get_sub_profiles()
    ]
    return vol.Schema(
        {
            vol.Required(CONF_SUB_PROFILE): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=sub_profiles, mode=selector.SelectSelectorMode.DROPDOWN
                )
            )
        }
    )


def _build_strategy_config(
    strategy: str, source_entity_id: str, user_input: dict[str, str] = None
) -> dict[str, Any]:
    """Build the config dict needed for the configured strategy"""
    strategy_schema = _get_strategy_schema(strategy, source_entity_id)
    strategy_options: dict[str, Any] = {}
    for key in strategy_schema.schema.keys():
        if user_input.get(key) is None:
            continue
        strategy_options[str(key)] = user_input.get(key)
    return strategy_options


def _build_daily_energy_config(user_input: dict[str, str] = None) -> dict[str, Any]:
    """Build the config under daily_energy: key"""
    schema = SCHEMA_DAILY_ENERGY_OPTIONS
    config: dict[str, Any] = {}
    for key in schema.schema.keys():
        if user_input.get(key) is None:
            continue
        config[str(key)] = user_input.get(key)
    return config


def _validate_daily_energy_input(user_input: dict[str, str] = None) -> dict:
    """Validates the daily energy form"""
    if not user_input:
        return {}
    errors: dict[str, str] = {}

    if CONF_VALUE not in user_input and CONF_VALUE_TEMPLATE not in user_input:
        errors["base"] = "daily_energy_mandatory"

    return errors


def _fill_schema_defaults(data_schema: vol.Schema, options: dict[str, str]):
    """Make a copy of the schema with suggested values set to saved options"""
    schema = {}
    for key, val in data_schema.schema.items():
        new_key = key
        if key in options and isinstance(key, vol.Marker):
            if (
                isinstance(key, vol.Optional)
                and callable(key.default)
                and key.default()
            ):
                new_key = vol.Optional(key.schema, default=options.get(key))
            else:
                new_key = copy.copy(key)
                new_key.description = {"suggested_value": options.get(key)}
        schema[new_key] = val
    data_schema = vol.Schema(schema)
    return data_schema
