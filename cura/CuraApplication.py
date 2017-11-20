# Copyright (c) 2017 Ultimaker B.V.
# Cura is released under the terms of the LGPLv3 or higher.

from PyQt5.QtNetwork import QLocalServer
from PyQt5.QtNetwork import QLocalSocket

from UM.Qt.QtApplication import QtApplication
from UM.Scene.SceneNode import SceneNode
from UM.Scene.Camera import Camera
from UM.Math.Vector import Vector
from UM.Math.Quaternion import Quaternion
from UM.Math.AxisAlignedBox import AxisAlignedBox
from UM.Math.Matrix import Matrix
from UM.Resources import Resources
from UM.Scene.ToolHandle import ToolHandle
from UM.Scene.Iterator.DepthFirstIterator import DepthFirstIterator
from UM.Mesh.ReadMeshJob import ReadMeshJob
from UM.Logger import Logger
from UM.Preferences import Preferences
from UM.SaveFile import SaveFile
from UM.Scene.Selection import Selection
from UM.Scene.GroupDecorator import GroupDecorator
from UM.Settings.ContainerStack import ContainerStack
from UM.Settings.InstanceContainer import InstanceContainer
from UM.Settings.Validator import Validator
from UM.Message import Message
from UM.i18n import i18nCatalog
from UM.Workspace.WorkspaceReader import WorkspaceReader
from UM.Decorators import deprecated

from UM.Operations.AddSceneNodeOperation import AddSceneNodeOperation
from UM.Operations.RemoveSceneNodeOperation import RemoveSceneNodeOperation
from UM.Operations.GroupedOperation import GroupedOperation
from UM.Operations.SetTransformOperation import SetTransformOperation
from cura.Arrange import Arrange
from cura.ShapeArray import ShapeArray
from cura.ConvexHullDecorator import ConvexHullDecorator
from cura.SetParentOperation import SetParentOperation
from cura.SliceableObjectDecorator import SliceableObjectDecorator
from cura.BlockSlicingDecorator import BlockSlicingDecorator

from cura.ArrangeObjectsJob import ArrangeObjectsJob
from cura.MultiplyObjectsJob import MultiplyObjectsJob

from UM.Settings.SettingDefinition import SettingDefinition, DefinitionPropertyType
from UM.Settings.ContainerRegistry import ContainerRegistry
from UM.Settings.SettingFunction import SettingFunction
from cura.Settings.MachineNameValidator import MachineNameValidator
from cura.Settings.ProfilesModel import ProfilesModel
from cura.Settings.MaterialsModel import MaterialsModel
from cura.Settings.QualityAndUserProfilesModel import QualityAndUserProfilesModel
from cura.Settings.SettingInheritanceManager import SettingInheritanceManager
from cura.Settings.UserProfilesModel import UserProfilesModel
from cura.Settings.SimpleModeSettingsManager import SimpleModeSettingsManager

import time

from . import PlatformPhysics
from . import BuildVolume
from . import CameraAnimation
from . import PrintInformation
from . import CuraActions
from . import ZOffsetDecorator
from . import CuraSplashScreen
from . import CameraImageProvider
from . import MachineActionManager

from cura.Settings.MachineManager import MachineManager
from cura.Settings.MaterialManager import MaterialManager
from cura.Settings.ExtruderManager import ExtruderManager
from cura.Settings.UserChangesModel import UserChangesModel
from cura.Settings.ExtrudersModel import ExtrudersModel
from cura.Settings.ContainerSettingsModel import ContainerSettingsModel
from cura.Settings.MaterialSettingsVisibilityHandler import MaterialSettingsVisibilityHandler
from cura.Settings.QualitySettingsModel import QualitySettingsModel
from cura.Settings.ContainerManager import ContainerManager
from cura.Settings.GlobalStack import GlobalStack
from cura.Settings.ExtruderStack import ExtruderStack

from PyQt5.QtCore import QUrl, pyqtSignal, pyqtProperty, QEvent, Q_ENUMS
from UM.FlameProfiler import pyqtSlot
from PyQt5.QtGui import QColor, QIcon
from PyQt5.QtWidgets import QMessageBox
from PyQt5.QtQml import qmlRegisterUncreatableType, qmlRegisterSingletonType, qmlRegisterType

import sys
import os.path
import numpy
import copy
import urllib.parse
import os
import argparse
import json
import signal

numpy.seterr(all="ignore")

MYPY = False
if not MYPY:
    try:
        from cura.CuraVersion import CuraVersion, CuraBuildType
    except ImportError:
        CuraVersion = "master"  # [CodeStyle: Reflecting imported value]
        CuraBuildType = ""


class CuraApplication(QtApplication):
    # SettingVersion represents the set of settings available in the machine/extruder definitions.
    # You need to make sure that this version number needs to be increased if there is any non-backwards-compatible
    # changes of the settings.
    SettingVersion = 4

    class ResourceTypes:
        QmlFiles = Resources.UserType + 1
        Firmware = Resources.UserType + 2
        QualityInstanceContainer = Resources.UserType + 3
        MaterialInstanceContainer = Resources.UserType + 4
        VariantInstanceContainer = Resources.UserType + 5
        UserInstanceContainer = Resources.UserType + 6
        MachineStack = Resources.UserType + 7
        ExtruderStack = Resources.UserType + 8
        DefinitionChangesContainer = Resources.UserType + 9

    Q_ENUMS(ResourceTypes)

    # FIXME: This signal belongs to the MachineManager, but the CuraEngineBackend plugin requires on it.
    #        Because plugins are initialized before the ContainerRegistry, putting this signal in MachineManager
    #        will make it initialized before ContainerRegistry does, and it won't find the active machine, thus
    #        Cura will always show the Add Machine Dialog upon start.
    stacksValidationFinished = pyqtSignal()  # Emitted whenever a validation is finished

    projectFileLoaded = pyqtSignal(str)  # Emitted whenever a project file is loaded

    def __init__(self):
        # this list of dir names will be used by UM to detect an old cura directory
        for dir_name in ["extruders", "machine_instances", "materials", "plugins", "quality", "user", "variants"]:
            Resources.addExpectedDirNameInData(dir_name)

        Resources.addSearchPath(os.path.join(QtApplication.getInstallPrefix(), "share", "cura", "resources"))
        if not hasattr(sys, "frozen"):
            Resources.addSearchPath(os.path.join(os.path.abspath(os.path.dirname(__file__)), "..", "resources"))

        self._open_file_queue = []  # Files to open when plug-ins are loaded.

        # Need to do this before ContainerRegistry tries to load the machines
        SettingDefinition.addSupportedProperty("settable_per_mesh", DefinitionPropertyType.Any, default = True, read_only = True)
        SettingDefinition.addSupportedProperty("settable_per_extruder", DefinitionPropertyType.Any, default = True, read_only = True)
        # this setting can be changed for each group in one-at-a-time mode
        SettingDefinition.addSupportedProperty("settable_per_meshgroup", DefinitionPropertyType.Any, default = True, read_only = True)
        SettingDefinition.addSupportedProperty("settable_globally", DefinitionPropertyType.Any, default = True, read_only = True)

        # From which stack the setting would inherit if not defined per object (handled in the engine)
        # AND for settings which are not settable_per_mesh:
        # which extruder is the only extruder this setting is obtained from
        SettingDefinition.addSupportedProperty("limit_to_extruder", DefinitionPropertyType.Function, default = "-1", depends_on = "value")

        # For settings which are not settable_per_mesh and not settable_per_extruder:
        # A function which determines the glabel/meshgroup value by looking at the values of the setting in all (used) extruders
        SettingDefinition.addSupportedProperty("resolve", DefinitionPropertyType.Function, default = None, depends_on = "value")

        SettingDefinition.addSettingType("extruder", None, str, Validator)
        SettingDefinition.addSettingType("optional_extruder", None, str, None)

        SettingDefinition.addSettingType("[int]", None, str, None)

        SettingFunction.registerOperator("extruderValues", ExtruderManager.getExtruderValues)
        SettingFunction.registerOperator("extruderValue", ExtruderManager.getExtruderValue)
        SettingFunction.registerOperator("resolveOrValue", ExtruderManager.getResolveOrValue)

        ## Add the 4 types of profiles to storage.
        Resources.addStorageType(self.ResourceTypes.QualityInstanceContainer, "quality")
        Resources.addStorageType(self.ResourceTypes.VariantInstanceContainer, "variants")
        Resources.addStorageType(self.ResourceTypes.MaterialInstanceContainer, "materials")
        Resources.addStorageType(self.ResourceTypes.UserInstanceContainer, "user")
        Resources.addStorageType(self.ResourceTypes.ExtruderStack, "extruders")
        Resources.addStorageType(self.ResourceTypes.MachineStack, "machine_instances")
        Resources.addStorageType(self.ResourceTypes.DefinitionChangesContainer, "definition_changes")

        ContainerRegistry.getInstance().addResourceType(self.ResourceTypes.QualityInstanceContainer)
        ContainerRegistry.getInstance().addResourceType(self.ResourceTypes.VariantInstanceContainer)
        ContainerRegistry.getInstance().addResourceType(self.ResourceTypes.MaterialInstanceContainer)
        ContainerRegistry.getInstance().addResourceType(self.ResourceTypes.UserInstanceContainer)
        ContainerRegistry.getInstance().addResourceType(self.ResourceTypes.ExtruderStack)
        ContainerRegistry.getInstance().addResourceType(self.ResourceTypes.MachineStack)
        ContainerRegistry.getInstance().addResourceType(self.ResourceTypes.DefinitionChangesContainer)

        ##  Initialise the version upgrade manager with Cura's storage paths.
        import UM.VersionUpgradeManager #Needs to be here to prevent circular dependencies.

        UM.VersionUpgradeManager.VersionUpgradeManager.getInstance().setCurrentVersions(
            {
                ("quality_changes", InstanceContainer.Version * 1000000 + self.SettingVersion):    (self.ResourceTypes.QualityInstanceContainer, "application/x-uranium-instancecontainer"),
                ("machine_stack", ContainerStack.Version * 1000000 + self.SettingVersion): (self.ResourceTypes.MachineStack, "application/x-cura-globalstack"),
                ("extruder_train", ContainerStack.Version * 1000000 + self.SettingVersion): (self.ResourceTypes.ExtruderStack, "application/x-cura-extruderstack"),
                ("preferences", Preferences.Version * 1000000 + self.SettingVersion):               (Resources.Preferences, "application/x-uranium-preferences"),
                ("user", InstanceContainer.Version * 1000000 + self.SettingVersion):       (self.ResourceTypes.UserInstanceContainer, "application/x-uranium-instancecontainer"),
                ("definition_changes", InstanceContainer.Version * 1000000 + self.SettingVersion): (self.ResourceTypes.DefinitionChangesContainer, "application/x-uranium-instancecontainer"),
            }
        )

        self._currently_loading_files = []
        self._non_sliceable_extensions = []
        self._print_monitor_additional_sections = []
        Logger.log("d", "QtApplication Install Prefix : \"" + str(QtApplication.getInstallPrefix()) + "\"")
        try:
             self._components_version = json.load(open("version.json", "r"))
        except:
             try:
                  self._components_version = json.load(open(
                       os.path.join(QtApplication.getInstallPrefix(), "version.json"), "r"))
             except:
                  try:
                       self._components_version = json.load(open(
                            os.path.join(QtApplication.getInstallPrefix(), "cura-lulzbot","version.json"), "r"))
                  except:
                       self._components_version = {"cura_version": "master"}

        self._machine_action_manager = MachineActionManager.MachineActionManager()
        self._machine_manager = None    # This is initialized on demand.
        self._material_manager = None
        self._setting_inheritance_manager = None
        self._simple_mode_settings_manager = None

        self._additional_components = {} # Components to add to certain areas in the interface

        Preferences.getInstance().addPreference("info/automatic_update_check", False)

        super().__init__(name = "cura-lulzbot", version = self.getComponentVersion("cura_version"), buildtype = CuraBuildType, tray_icon_name = "cura-icon.png")

        self.default_theme = "lulzbot"

        Logger.log("d", "Trying to Set icon : \"" + str(Resources.getPath(Resources.Images, "cura-icon.png"))  + "\"")
        self.setWindowIcon(QIcon(Resources.getPath(Resources.Images, "cura-icon.png")))

        self.setRequiredPlugins([
            "CuraEngineBackend",
            "UserAgreement",
            "SolidView",
            "LayerView",
            "STLReader",
            "SelectionTool",
            "CameraTool",
            "GCodeWriter",
            "LocalFileOutputDevice",
            "SolidView",
            "TranslateTool",
            "FileLogger",
            "XmlMaterialProfile",
            "PluginBrowser"
        ])
        self._physics = None
        self._volume = None
        self._output_devices = {}
        self._print_information = None
        self._previous_active_tool = None
        self._previous_active_tool_time = time.time()
        self._platform_activity = False
        self._scene_bounding_box = AxisAlignedBox.Null

        self._job_name = None
        self._center_after_select = False
        self._camera_animation = None
        self._cura_actions = None
        self._started = False

        self._message_box_callback = None
        self._message_box_callback_arguments = []
        self._preferred_mimetype = ""
        self._i18n_catalog = i18nCatalog("cura")

        self.getController().getScene().sceneChanged.connect(self.updatePlatformActivity)
        self.getController().toolOperationStopped.connect(self._onToolOperationStopped)
        self.getController().contextMenuRequested.connect(self._onContextMenuRequested)

        Resources.addType(self.ResourceTypes.QmlFiles, "qml")
        Resources.addType(self.ResourceTypes.Firmware, "firmware")

        self.showSplashMessage(self._i18n_catalog.i18nc("@info:progress", "Loading machines..."))

        # Add empty variant, material and quality containers.
        # Since they are empty, they should never be serialized and instead just programmatically created.
        # We need them to simplify the switching between materials.
        empty_container = ContainerRegistry.getInstance().getEmptyInstanceContainer()
        empty_variant_container = copy.deepcopy(empty_container)
        empty_variant_container._id = "empty_variant"
        empty_variant_container.addMetaDataEntry("type", "variant")
        ContainerRegistry.getInstance().addContainer(empty_variant_container)
        empty_material_container = copy.deepcopy(empty_container)
        empty_material_container._id = "empty_material"
        empty_material_container.addMetaDataEntry("type", "material")
        ContainerRegistry.getInstance().addContainer(empty_material_container)
        empty_quality_container = copy.deepcopy(empty_container)
        empty_quality_container._id = "empty_quality"
        empty_quality_container.setName("Not Supported")
        empty_quality_container.addMetaDataEntry("quality_type", "not_supported")
        empty_quality_container.addMetaDataEntry("type", "quality")
        empty_quality_container.addMetaDataEntry("supported", False)
        ContainerRegistry.getInstance().addContainer(empty_quality_container)
        empty_quality_changes_container = copy.deepcopy(empty_container)
        empty_quality_changes_container._id = "empty_quality_changes"
        empty_quality_changes_container.addMetaDataEntry("type", "quality_changes")
        ContainerRegistry.getInstance().addContainer(empty_quality_changes_container)

        with ContainerRegistry.getInstance().lockFile():
            ContainerRegistry.getInstance().load()

        # set the setting version for Preferences
        preferences = Preferences.getInstance()
        preferences.addPreference("metadata/setting_version", 0)
        preferences.setValue("metadata/setting_version", self.SettingVersion) #Don't make it equal to the default so that the setting version always gets written to the file.

        preferences.addPreference("cura/active_mode", "simple")

        preferences.addPreference("cura/categories_expanded", "")
        preferences.addPreference("cura/jobname_prefix", True)
        preferences.addPreference("view/center_on_select", False)
        preferences.addPreference("mesh/scale_to_fit", False)
        preferences.addPreference("mesh/scale_tiny_meshes", True)
        preferences.addPreference("cura/dialog_on_project_save", True)
        preferences.addPreference("cura/asked_dialog_on_project_save", False)
        preferences.addPreference("cura/choice_on_profile_override", "always_ask")
        preferences.addPreference("cura/choice_on_open_project", "always_ask")

        preferences.addPreference("cura/currency", "€")
        preferences.addPreference("cura/material_settings", "{}")

        preferences.addPreference("view/invert_zoom", False)

        Preferences.getInstance().addPreference("cura/recent_files", "")
        self._need_to_show_user_agreement = not Preferences.getInstance().getValue("general/accepted_user_agreement")


        for key in [
            "dialog_load_path",  # dialog_save_path is in LocalFileOutputDevicePlugin
            "dialog_profile_path",
            "dialog_material_path"]:

            preferences.addPreference("local_file/%s" % key, os.path.expanduser("~/"))

        preferences.setDefault("local_file/last_used_type", "text/x-gcode")

        preferences.setDefault("info/automatic_update_check", False)

        preferences.setDefault("general/visible_settings", """
            machine_settings
            resolution
                layer_height
            shell
                wall_thickness
                top_bottom_thickness
                z_seam_x
                z_seam_y
            infill
                infill_sparse_density
                gradual_infill_steps
            material
                material_print_temperature
                    material_soften_temperature
                    material_probe_temperature
                    material_wipe_temperature
                material_bed_temperature
                    material_part_removal_temperature
                    material_keep_part_removal_temperature
                material_diameter
                material_flow
                retraction_enable
            speed
                speed_print
                speed_travel
                acceleration_print
                acceleration_travel
                jerk_print
                jerk_travel
            travel
            cooling
                cool_fan_enabled
            support
                support_enable
                support_extruder_nr
                support_type
            platform_adhesion
                adhesion_type
                adhesion_extruder_nr
                brim_width
                raft_airgap
                layer_0_z_overlap
                raft_surface_layers
            dual
                prime_tower_enable
                prime_tower_size
                prime_tower_position_x
                prime_tower_position_y
            meshfix
            blackmagic
                print_sequence
                infill_mesh
                cutting_mesh
            experimental
            top_bottom_pattern_0;travel_compensate_overlapping_walls_enabled;travel_compensate_overlapping_walls_x_enabled;travel_compensate_overlapping_walls_0_enabled;skin_outline_count;wall_thickness;wall_line_count;wall_0_inset;top_bottom_thickness;bottom_thickness;bottom_layers;top_thickness;top_layers;wall_extruder_nr;wall_x_extruder_nr;wall_0_extruder_nr;z_seam_y;top_bottom_extruder_nr;top_bottom_pattern;z_seam_type;roofing_extruder_nr;wall_0_wipe_dist;skin_angles;z_seam_corner;roofing_pattern;outer_inset_first;xy_offset;skin_no_small_gaps_heuristic;z_seam_x;fill_outline_gaps;roofing_angles;fill_perimeter_gaps;alternate_extra_perimeter;roofing_layer_count;xy_offset_layer_0;z_seam_relative;material_print_temperature_layer_0;retraction_count_max;material_bed_temperature_layer_0;retract_at_layer_change;switch_extruder_retraction_amount;switch_extruder_retraction_speeds;switch_extruder_prime_speed;switch_extruder_retraction_speed;retraction_extrusion_window;retraction_amount;retraction_min_travel;material_initial_print_temperature;material_adhesion_tendency;material_flow;material_flow_temp_graph;material_final_print_temperature;material_diameter;retraction_enable;material_bed_temperature;material_standby_temperature;material_extrusion_cool_down_speed;material_print_temperature;material_surface_energy;retraction_extra_prime_amount;material_flow_dependent_temperature;retraction_speed;retraction_prime_speed;retraction_retract_speed;default_material_print_temperature;cool_lift_head;cool_fan_full_at_height;cool_fan_full_layer;cool_min_layer_time;cool_fan_speed_0;cool_min_layer_time_fan_speed_max;cool_fan_enabled;cool_fan_speed;cool_fan_speed_min;cool_fan_speed_max;cool_min_speed;machine_nozzle_heat_up_speed;machine_heated_bed;material_bed_temp_wait;machine_height;machine_gcode_flavor;gantry_height;machine_min_cool_heat_time_window;material_print_temp_wait;machine_nozzle_tip_outer_diameter;machine_max_feedrate_x;machine_filament_park_distance;machine_use_extruder_offset_to_offset_coords;machine_acceleration;machine_wipe_gcode;material_bed_temp_prepend;machine_head_with_fans_polygon;machine_port;machine_max_feedrate_e;machine_max_jerk_e;machine_nozzle_size;machine_width;machine_nozzle_temp_enabled;machine_heat_zone_length;machine_max_jerk_z;material_print_temp_prepend;machine_depth;extruder_prime_pos_z;machine_head_polygon;machine_start_gcode;machine_shape;machine_center_is_zero;machine_nozzle_head_distance;machine_nozzle_expansion_angle;machine_max_acceleration_z;machine_show_variants;machine_max_feedrate_z;machine_nozzle_cool_down_speed;machine_minimum_feedrate;machine_baudrate;machine_max_acceleration_y;machine_name;machine_max_feedrate_y;machine_max_jerk_xy;machine_nozzle_id;material_guid;extruder_prime_pos_abs;machine_disallowed_areas;machine_max_acceleration_x;machine_max_acceleration_e;machine_end_gcode;machine_extruder_count;nozzle_disallowed_areas;meshfix_union_all;alternate_carve_order;carve_multiple_volumes;multiple_mesh_overlap;meshfix_union_all_remove_holes;meshfix_keep_open_polygons;remove_empty_first_layers;meshfix_extensive_stitching;prime_tower_size;prime_tower_enable;prime_tower_position_y;prime_tower_min_volume;prime_tower_wall_thickness;ooze_shield_enabled;prime_tower_position_x;prime_tower_wipe_enabled;dual_pre_wipe;ooze_shield_dist;ooze_shield_angle;prime_tower_flow;prime_tower_purge_volume;relative_extrusion;support_mesh;print_sequence;smooth_spiralized_contours;mold_enabled;magic_spiralize;magic_mesh_surface_mode;support_mesh_drop_down;cutting_mesh;mold_roof_height;infill_mesh_order;infill_mesh;mold_angle;mold_width;anti_overhang_mesh;mesh_position_y;center_object;mesh_position_x;mesh_rotation_matrix;mesh_position_z;retraction_combing;travel_retract_before_outer_wall;retraction_hop_after_extruder_switch;retraction_hop_enabled;travel_avoid_other_parts;retraction_hop_only_when_collides;retraction_hop;layer_start_y;layer_start_x;travel_avoid_distance;start_layers_at_same_position;support_interface_pattern;support_bottom_pattern;support_roof_pattern;support_tower_roof_angle;support_interface_skip_height;support_xy_overrides_z;support_interface_height;support_roof_height;support_bottom_height;support_join_distance;support_minimal_diameter;support_enable;support_connect_zigzags;support_extruder_nr;support_extruder_nr_layer_0;support_infill_extruder_nr;support_interface_extruder_nr;support_bottom_extruder_nr;support_roof_extruder_nr;support_interface_enable;support_bottom_enable;support_roof_enable;support_offset;support_xy_distance;gradual_support_infill_step_height;support_xy_distance_overhang;support_infill_rate;support_line_distance;support_bottom_stair_step_height;support_z_distance;support_top_distance;support_bottom_distance;support_bottom_stair_step_width;support_infill_sparse_thickness;support_tower_diameter;support_pattern;support_angle;support_type;support_interface_density;support_roof_density;support_roof_line_distance;support_bottom_density;support_bottom_line_distance;support_use_towers;gradual_support_infill_steps;speed_equalize_flow_enabled;acceleration_layer_0;acceleration_travel_layer_0;acceleration_print_layer_0;jerk_travel;acceleration_skirt_brim;jerk_print;jerk_wall;jerk_wall_x;jerk_wall_0;jerk_roofing;jerk_support;jerk_support_interface;jerk_support_roof;jerk_support_bottom;jerk_support_infill;jerk_topbottom;jerk_prime_tower;jerk_infill;speed_equalize_flow_max;acceleration_print;acceleration_roofing;acceleration_topbottom;acceleration_support;acceleration_support_infill;acceleration_support_interface;acceleration_support_roof;acceleration_support_bottom;acceleration_wall;acceleration_wall_0;acceleration_wall_x;acceleration_infill;acceleration_prime_tower;jerk_skirt_brim;skirt_brim_speed;speed_travel;max_feedrate_z_override;acceleration_travel;acceleration_enabled;jerk_layer_0;jerk_print_layer_0;jerk_travel_layer_0;speed_slowdown_layers;jerk_enabled;speed_layer_0;speed_print_layer_0;speed_travel_layer_0;speed_print;speed_prime_tower;speed_wall;speed_wall_x;speed_wall_0;speed_roofing;speed_infill;speed_support;speed_support_interface;speed_support_bottom;speed_support_roof;speed_support_infill;speed_topbottom;layer_height;slicing_tolerance;line_width;wall_line_width;wall_line_width_x;wall_line_width_0;support_interface_line_width;support_roof_line_width;support_bottom_line_width;roofing_line_width;initial_layer_line_width_factor;skin_line_width;support_line_width;infill_line_width;skirt_brim_line_width;prime_tower_line_width;layer_height_0;speed_ironing;optimize_wall_printing_order;ironing_pattern;spaghetti_inset;skin_alternate_rotation;wireframe_top_delay;spaghetti_infill_enabled;cross_infill_apply_pockets_alternatingly;wireframe_roof_outer_delay;spaghetti_infill_stepped;wireframe_roof_drag_along;coasting_enable;ironing_inset;wireframe_enabled;support_skip_zag_per_mm;support_zag_skip_count;conical_overhang_enabled;draft_shield_dist;wireframe_roof_fall_down;coasting_volume;wireframe_top_jump;spaghetti_flow;coasting_speed;wireframe_flow;wireframe_flow_connection;wireframe_flow_flat;wireframe_fall_down;cross_infill_pocket_size;wireframe_strategy;wireframe_printspeed;wireframe_printspeed_bottom;wireframe_printspeed_up;wireframe_printspeed_flat;wireframe_printspeed_down;spaghetti_max_infill_angle;spaghetti_infill_extra_volume;wireframe_roof_inset;wireframe_height;coasting_min_volume;wireframe_flat_delay;spaghetti_max_height;draft_shield_height;magic_fuzzy_skin_enabled;magic_fuzzy_skin_thickness;draft_shield_enabled;acceleration_ironing;ironing_flow;wireframe_bottom_delay;support_conical_enabled;wireframe_up_half_speed;magic_fuzzy_skin_point_density;magic_fuzzy_skin_point_dist;support_conical_min_width;wireframe_nozzle_clearance;jerk_ironing;ironing_enabled;conical_overhang_angle;ironing_line_spacing;draft_shield_height_limitation;infill_hollow;support_conical_angle;wireframe_straight_before_down;support_skip_some_zags;ironing_only_highest_layer;wireframe_drag_along;skirt_line_count;brim_width;brim_line_count;extruder_prime_pos_x;raft_base_line_width;raft_surface_line_spacing;raft_acceleration;raft_interface_acceleration;raft_surface_acceleration;raft_base_acceleration;raft_base_thickness;raft_interface_line_width;raft_speed;raft_surface_speed;raft_interface_speed;raft_base_speed;raft_margin;raft_airgap;raft_surface_layers;adhesion_type;raft_smoothing;skirt_gap;skirt_brim_minimal_length;raft_fan_speed;raft_surface_fan_speed;raft_interface_fan_speed;raft_base_fan_speed;raft_interface_line_spacing;layer_0_z_overlap;raft_jerk;raft_base_jerk;raft_interface_jerk;raft_surface_jerk;raft_surface_line_width;prime_blob_enable;raft_interface_thickness;raft_surface_thickness;adhesion_extruder_nr;raft_base_line_spacing;extruder_prime_pos_y;brim_outside_only;infill_pattern;infill_wipe_dist;skin_overlap;skin_overlap_mm;expand_skins_expand_distance;top_skin_expand_distance;bottom_skin_expand_distance;infill_sparse_density;infill_line_distance;max_skin_angle_for_expansion;min_skin_width_for_expansion;infill_overlap;infill_overlap_mm;infill_extruder_nr;gradual_infill_step_height;infill_sparse_thickness;gradual_infill_steps;infill_angles;sub_div_rad_add;infill_offset_y;infill_offset_x;skin_preshrink;top_skin_preshrink;bottom_skin_preshrink;min_infill_area;zig_zaggify_infill;infill_before_walls;
        """.replace("\n", ";").replace(" ", ""))

        self.applicationShuttingDown.connect(self.saveSettings)
        self.engineCreatedSignal.connect(self._onEngineCreated)

        self._recent_files = []
        files = Preferences.getInstance().getValue("cura/recent_files").split(";")
        for f in files:
            if not os.path.isfile(f):
                continue

            self._recent_files.append(QUrl.fromLocalFile(f))

        self._exit_allowed = False
        self._original_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self.consoleExit)
        self.globalContainerStackChanged.connect(self._onGlobalContainerChanged)
        self._onGlobalContainerChanged()
        self._plugin_registry.addSupportedPluginExtension("curaplugin", "Cura Plugin")

    @pyqtSlot(str, result=str)
    def getComponentVersion(self, component):
        return self._components_version.get(component, "unknown")

    def _onEngineCreated(self):
        self._engine.addImageProvider("camera", CameraImageProvider.CameraImageProvider())

    @pyqtProperty(bool)
    def needToShowUserAgreement(self):
        return self._need_to_show_user_agreement


    def setNeedToShowUserAgreement(self, set_value = True):
        self._need_to_show_user_agreement = set_value

    ## The "Quit" button click event handler.
    @pyqtSlot()
    def closeApplication(self):
        Logger.log("i", "Close application")
        self._main_window.close()

    ## A reusable dialogbox
    #
    showMessageBox = pyqtSignal(str, str, str, str, int, int, arguments = ["title", "text", "informativeText", "detailedText", "buttons", "icon"])

    def messageBox(self, title, text, informativeText = "", detailedText = "", buttons = QMessageBox.Ok, icon = QMessageBox.NoIcon, callback = None, callback_arguments = []):
        self._message_box_callback = callback
        self._message_box_callback_arguments = callback_arguments
        self.showMessageBox.emit(title, text, informativeText, detailedText, buttons, icon)

    showDiscardOrKeepProfileChanges = pyqtSignal()

    def discardOrKeepProfileChanges(self):
        has_user_interaction = False
        choice = Preferences.getInstance().getValue("cura/choice_on_profile_override")
        if choice == "always_discard":
            # don't show dialog and DISCARD the profile
            self.discardOrKeepProfileChangesClosed("discard")
        elif choice == "always_keep":
            # don't show dialog and KEEP the profile
            self.discardOrKeepProfileChangesClosed("keep")
        elif len(self.getGlobalContainerStack().getTop().getAllKeys()) > 0:
            # ALWAYS ask whether to keep or discard the profile
            self.showDiscardOrKeepProfileChanges.emit()
            has_user_interaction = True
        return has_user_interaction

    onDiscardOrKeepProfileChangesClosed = pyqtSignal()  # Used to notify other managers that the dialog was closed

    @pyqtSlot(str)
    def discardOrKeepProfileChangesClosed(self, option):
        if option == "discard":
            global_stack = self.getGlobalContainerStack()
            for extruder in ExtruderManager.getInstance().getMachineExtruders(global_stack.getId()):
                extruder.getTop().clear()
            global_stack.getTop().clear()

        # if the user decided to keep settings then the user settings should be re-calculated and validated for errors
        # before slicing. To ensure that slicer uses right settings values
        elif option == "keep":
            global_stack = self.getGlobalContainerStack()
            for extruder in ExtruderManager.getInstance().getMachineExtruders(global_stack.getId()):
                user_extruder_container = extruder.getTop()
                if user_extruder_container:
                    user_extruder_container.update()

            user_global_container = global_stack.getTop()
            if user_global_container:
                user_global_container.update()

        # notify listeners that quality has changed (after user selected discard or keep)
        self.onDiscardOrKeepProfileChangesClosed.emit()
        self.getMachineManager().activeQualityChanged.emit()

    @pyqtSlot(int)
    def messageBoxClosed(self, button):
        if self._message_box_callback:
            self._message_box_callback(button, *self._message_box_callback_arguments)
            self._message_box_callback = None
            self._message_box_callback_arguments = []

    showPrintMonitor = pyqtSignal(bool, arguments = ["show"])

    def registerPrintMonitorAdditionalCategory(self, name, path):
        self._print_monitor_additional_sections.append({"name": name, "path": path})
        self.printMonitorAdditionalSectionsChanged.emit()

    printMonitorAdditionalSectionsChanged = pyqtSignal()

    @pyqtProperty("QVariantList", notify=printMonitorAdditionalSectionsChanged)
    def printMonitorAdditionalSections(self):
        return self._print_monitor_additional_sections

    ##  Cura has multiple locations where instance containers need to be saved, so we need to handle this differently.
    #
    #   Note that the AutoSave plugin also calls this method.
    def saveSettings(self):
        if not self._started: # Do not do saving during application start
            return

        # Lock file for "more" atomically loading and saving to/from config dir.
        with ContainerRegistry.getInstance().lockFile():
            for instance in ContainerRegistry.getInstance().findInstanceContainers():
                if not instance.isDirty():
                    continue

                try:
                    data = instance.serialize()
                except NotImplementedError:
                    continue
                except Exception:
                    Logger.logException("e", "An exception occurred when serializing container %s", instance.getId())
                    continue

                mime_type = ContainerRegistry.getMimeTypeForContainer(type(instance))
                file_name = urllib.parse.quote_plus(instance.getId()) + "." + mime_type.preferredSuffix
                instance_type = instance.getMetaDataEntry("type")
                path = None
                if instance_type == "material":
                    path = Resources.getStoragePath(self.ResourceTypes.MaterialInstanceContainer, file_name)
                elif instance_type == "quality" or instance_type == "quality_changes":
                    path = Resources.getStoragePath(self.ResourceTypes.QualityInstanceContainer, file_name)
                elif instance_type == "user":
                    path = Resources.getStoragePath(self.ResourceTypes.UserInstanceContainer, file_name)
                elif instance_type == "variant":
                    path = Resources.getStoragePath(self.ResourceTypes.VariantInstanceContainer, file_name)
                elif instance_type == "definition_changes":
                    path = Resources.getStoragePath(self.ResourceTypes.DefinitionChangesContainer, file_name)

                if path:
                    instance.setPath(path)
                    with SaveFile(path, "wt") as f:
                        f.write(data)

            for stack in ContainerRegistry.getInstance().findContainerStacks():
                self.saveStack(stack)

    def saveStack(self, stack):
        if not stack.isDirty():
            return
        try:
            data = stack.serialize()
        except NotImplementedError:
            return
        except Exception:
            Logger.logException("e", "An exception occurred when serializing container %s", stack.getId())
            return

        mime_type = ContainerRegistry.getMimeTypeForContainer(type(stack))
        file_name = urllib.parse.quote_plus(stack.getId()) + "." + mime_type.preferredSuffix

        path = None
        if isinstance(stack, GlobalStack):
            path = Resources.getStoragePath(self.ResourceTypes.MachineStack, file_name)
        elif isinstance(stack, ExtruderStack):
            path = Resources.getStoragePath(self.ResourceTypes.ExtruderStack, file_name)
        else:
            path = Resources.getStoragePath(Resources.ContainerStacks, file_name)

        stack.setPath(path)
        with SaveFile(path, "wt") as f:
            f.write(data)


    @pyqtSlot(str, result = QUrl)
    def getDefaultPath(self, key):
        default_path = Preferences.getInstance().getValue("local_file/%s" % key)
        return QUrl.fromLocalFile(default_path)

    @pyqtSlot(str, str)
    def setDefaultPath(self, key, default_path):
        Preferences.getInstance().setValue("local_file/%s" % key, QUrl(default_path).toLocalFile())

    @classmethod
    def getStaticVersion(cls):
        return CuraVersion

    ##  Handle loading of all plugin types (and the backend explicitly)
    #   \sa PluginRegistery
    def _loadPlugins(self):
        self._plugin_registry.addType("profile_reader", self._addProfileReader)
        self._plugin_registry.addType("profile_writer", self._addProfileWriter)
        self._plugin_registry.addPluginLocation(os.path.join(QtApplication.getInstallPrefix(), "lib", "cura"))
        if hasattr(sys, "frozen"):
            # This what works for MacOS currently
            self._plugin_registry.addPluginLocation(os.path.join(QtApplication.getInstallPrefix(), "Resources", "cura", "plugins"))
        else:
            self._plugin_registry.addPluginLocation(os.path.join(os.path.abspath(os.path.dirname(__file__)), "..", "plugins"))

        self._plugin_registry.loadPlugins()

        if self.getBackend() is None:
            raise RuntimeError("Could not load the backend plugin!")

        self._plugins_loaded = True

    @classmethod
    def addCommandLineOptions(self, parser):
        super().addCommandLineOptions(parser)
        parser.add_argument("file", nargs="*", help="Files to load after starting the application.")
        parser.add_argument("--single-instance", action="store_true", default=False)
        parser.add_argument("--headless", action = "store_true", default=False)

    # Set up a local socket server which listener which coordinates single instances Curas and accepts commands.
    def _setUpSingleInstanceServer(self):
        if self.getCommandLineOption("single_instance", False):
            self.__single_instance_server = QLocalServer()
            self.__single_instance_server.newConnection.connect(self._singleInstanceServerNewConnection)
            self.__single_instance_server.listen("ultimaker-cura")

    def _singleInstanceServerNewConnection(self):
        Logger.log("i", "New connection recevied on our single-instance server")
        remote_cura_connection = self.__single_instance_server.nextPendingConnection()

        if remote_cura_connection is not None:
            def readCommands():
                line = remote_cura_connection.readLine()
                while len(line) != 0:    # There is also a .canReadLine()
                    try:
                        payload = json.loads(str(line, encoding="ASCII").strip())
                        command = payload["command"]

                        # Command: Remove all models from the build plate.
                        if command == "clear-all":
                            self.deleteAll()

                        # Command: Load a model file
                        elif command == "open":
                            self._openFile(payload["filePath"])
                            # WARNING ^ this method is async and we really should wait until
                            # the file load is complete before processing more commands.

                        # Command: Activate the window and bring it to the top.
                        elif command == "focus":
                            # Operating systems these days prevent windows from moving around by themselves.
                            # 'alert' or flashing the icon in the taskbar is the best thing we do now.
                            self.getMainWindow().alert(0)

                        # Command: Close the socket connection. We're done.
                        elif command == "close-connection":
                            remote_cura_connection.close()

                        else:
                            Logger.log("w", "Received an unrecognized command " + str(command))
                    except json.decoder.JSONDecodeError as ex:
                        Logger.log("w", "Unable to parse JSON command in _singleInstanceServerNewConnection(): " + repr(ex))
                    line = remote_cura_connection.readLine()

            remote_cura_connection.readyRead.connect(readCommands)

    ##  Perform any checks before creating the main application.
    #
    #   This should be called directly before creating an instance of CuraApplication.
    #   \returns \type{bool} True if the whole Cura app should continue running.
    @classmethod
    def preStartUp(cls):
        # Peek the arguments and look for the 'single-instance' flag.
        parser = argparse.ArgumentParser(prog="cura")  # pylint: disable=bad-whitespace
        CuraApplication.addCommandLineOptions(parser)
        parsed_command_line = vars(parser.parse_args())

        if "single_instance" in parsed_command_line and parsed_command_line["single_instance"]:
            Logger.log("i", "Checking for the presence of an ready running Cura instance.")
            single_instance_socket = QLocalSocket()
            Logger.log("d", "preStartUp(): full server name: " + single_instance_socket.fullServerName())
            single_instance_socket.connectToServer("ultimaker-cura")
            single_instance_socket.waitForConnected()
            if single_instance_socket.state() == QLocalSocket.ConnectedState:
                Logger.log("i", "Connection has been made to the single-instance Cura socket.")

                # Protocol is one line of JSON terminated with a carriage return.
                # "command" field is required and holds the name of the command to execute.
                # Other fields depend on the command.

                payload = {"command": "clear-all"}
                single_instance_socket.write(bytes(json.dumps(payload) + "\n", encoding="ASCII"))

                payload = {"command": "focus"}
                single_instance_socket.write(bytes(json.dumps(payload) + "\n", encoding="ASCII"))

                if len(parsed_command_line["file"]) != 0:
                    for filename in parsed_command_line["file"]:
                        payload = {"command": "open", "filePath": filename}
                        single_instance_socket.write(bytes(json.dumps(payload) + "\n", encoding="ASCII"))

                payload = {"command": "close-connection"}
                single_instance_socket.write(bytes(json.dumps(payload) + "\n", encoding="ASCII"))

                single_instance_socket.flush()
                single_instance_socket.waitForDisconnected()
                return False
        return True

    def run(self):
        self.showSplashMessage(self._i18n_catalog.i18nc("@info:progress", "Setting up scene..."))

        self._setUpSingleInstanceServer()

        controller = self.getController()

        controller.setActiveView("SolidView")

        controller.setCameraTool("CameraTool")
        controller.setSelectionTool("SelectionTool")

        t = controller.getTool("TranslateTool")
        if t:
            t.setEnabledAxis([ToolHandle.XAxis, ToolHandle.YAxis,ToolHandle.ZAxis])

        Selection.selectionChanged.connect(self.onSelectionChanged)

        root = controller.getScene().getRoot()

        # The platform is a child of BuildVolume
        self._volume = BuildVolume.BuildVolume(root)

        # Set the build volume of the arranger to the used build volume
        Arrange.build_volume = self._volume

        self.getRenderer().setBackgroundColor(QColor(245, 245, 245))

        self._physics = PlatformPhysics.PlatformPhysics(controller, self._volume)

        camera = Camera("3d", root)
        camera.setPosition(Vector(-80, 250, 700))
        camera.setPerspective(True)
        camera.lookAt(Vector(0, 0, 0))
        controller.getScene().setActiveCamera("3d")

        camera_tool = self.getController().getTool("CameraTool")
        camera_tool.setOrigin(Vector(0, 100, 0))
        camera_tool.setZoomRange(0.1, 200000)

        self._camera_animation = CameraAnimation.CameraAnimation()
        self._camera_animation.setCameraTool(self.getController().getTool("CameraTool"))

        self.showSplashMessage(self._i18n_catalog.i18nc("@info:progress", "Loading interface..."))

        # Initialise extruder so as to listen to global container stack changes before the first global container stack is set.
        qmlRegisterSingletonType(ExtruderManager, "Cura", 1, 0, "ExtruderManager", self.getExtruderManager)
        qmlRegisterSingletonType(MachineManager, "Cura", 1, 0, "MachineManager", self.getMachineManager)
        qmlRegisterSingletonType(MaterialManager, "Cura", 1, 0, "MaterialManager", self.getMaterialManager)
        qmlRegisterSingletonType(SettingInheritanceManager, "Cura", 1, 0, "SettingInheritanceManager",
                                 self.getSettingInheritanceManager)
        qmlRegisterSingletonType(SimpleModeSettingsManager, "Cura", 1, 2, "SimpleModeSettingsManager",
                                 self.getSimpleModeSettingsManager)

        qmlRegisterSingletonType(MachineActionManager.MachineActionManager, "Cura", 1, 0, "MachineActionManager", self.getMachineActionManager)
        self.setMainQml(Resources.getPath(self.ResourceTypes.QmlFiles, "Cura.qml"))
        self._qml_import_paths.append(Resources.getPath(self.ResourceTypes.QmlFiles))

        run_headless = self.getCommandLineOption("headless", False)
        if not run_headless:
            self.initializeEngine()

        if run_headless or self._engine.rootObjects:
            self.closeSplash()

            for file_name in self.getCommandLineOption("file", []):
                self._openFile(file_name)
            for file_name in self._open_file_queue: #Open all the files that were queued up while plug-ins were loading.
                self._openFile(file_name)

            self._started = True

            self.exec_()

    def isExitAllowed(self):
        is_printing = len(self.getMachineManager().printerOutputDevices) > 0 and\
                      self.getMachineManager().printerOutputDevices[0].acceptsCommands and\
                      self.getMachineManager().printerOutputDevices[0].jobState in ["paused", "printing", "pre_print"]
        if not is_printing:
            return True
        if self._exit_allowed:
            return True
        self.exitRequested.emit()
        return False

    def consoleExit(self, signum, frame):
        signal.signal(signal.SIGINT, self._original_sigint)

        if self.isExitAllowed():
            self.windowClosed()

        signal.signal(signal.SIGINT, self.consoleExit)

    exitRequested = pyqtSignal()

    def setExitAllowed(self, allowed):
        self._exit_allowed = allowed

    @pyqtProperty(bool, fset=setExitAllowed)
    def exitAllowed(self):
        return self._exit_allowed

    def getMachineManager(self, *args):
        if self._machine_manager is None:
            self._machine_manager = MachineManager.createMachineManager()
        return self._machine_manager

    def getExtruderManager(self, *args):
        return ExtruderManager.getInstance()

    def getMaterialManager(self, *args):
        if self._material_manager is None:
            self._material_manager = MaterialManager.createMaterialManager()
        return self._material_manager

    def getSettingInheritanceManager(self, *args):
        if self._setting_inheritance_manager is None:
            self._setting_inheritance_manager = SettingInheritanceManager.createSettingInheritanceManager()
        return self._setting_inheritance_manager

    ##  Get the machine action manager
    #   We ignore any *args given to this, as we also register the machine manager as qml singleton.
    #   It wants to give this function an engine and script engine, but we don't care about that.
    def getMachineActionManager(self, *args):
        return self._machine_action_manager

    def getSimpleModeSettingsManager(self, *args):
        if self._simple_mode_settings_manager is None:
            self._simple_mode_settings_manager = SimpleModeSettingsManager()
        return self._simple_mode_settings_manager

    ##   Handle Qt events
    def event(self, event):
        if event.type() == QEvent.FileOpen:
            if self._plugins_loaded:
                self._openFile(event.file())
            else:
                self._open_file_queue.append(event.file())

        return super().event(event)

    ##  Get print information (duration / material used)
    def getPrintInformation(self):
        return self._print_information

    ##  Registers objects for the QML engine to use.
    #
    #   \param engine The QML engine.
    def registerObjects(self, engine):
        super().registerObjects(engine)
        engine.rootContext().setContextProperty("Printer", self)
        engine.rootContext().setContextProperty("CuraApplication", self)
        self._print_information = PrintInformation.PrintInformation()
        engine.rootContext().setContextProperty("PrintInformation", self._print_information)
        self._cura_actions = CuraActions.CuraActions(self)
        engine.rootContext().setContextProperty("CuraActions", self._cura_actions)

        qmlRegisterUncreatableType(CuraApplication, "Cura", 1, 0, "ResourceTypes", "Just an Enum type")

        qmlRegisterType(ExtrudersModel, "Cura", 1, 0, "ExtrudersModel")

        qmlRegisterType(ContainerSettingsModel, "Cura", 1, 0, "ContainerSettingsModel")
        qmlRegisterSingletonType(ProfilesModel, "Cura", 1, 0, "ProfilesModel", ProfilesModel.createProfilesModel)
        qmlRegisterType(MaterialsModel, "Cura", 1, 0, "MaterialsModel")
        qmlRegisterType(QualityAndUserProfilesModel, "Cura", 1, 0, "QualityAndUserProfilesModel")
        qmlRegisterType(UserProfilesModel, "Cura", 1, 0, "UserProfilesModel")
        qmlRegisterType(MaterialSettingsVisibilityHandler, "Cura", 1, 0, "MaterialSettingsVisibilityHandler")
        qmlRegisterType(QualitySettingsModel, "Cura", 1, 0, "QualitySettingsModel")
        qmlRegisterType(MachineNameValidator, "Cura", 1, 0, "MachineNameValidator")
        qmlRegisterType(UserChangesModel, "Cura", 1, 1, "UserChangesModel")

        qmlRegisterSingletonType(ContainerManager, "Cura", 1, 0, "ContainerManager", ContainerManager.createContainerManager)

        # As of Qt5.7, it is necessary to get rid of any ".." in the path for the singleton to work.
        actions_url = QUrl.fromLocalFile(os.path.abspath(Resources.getPath(CuraApplication.ResourceTypes.QmlFiles, "Actions.qml")))
        qmlRegisterSingletonType(actions_url, "Cura", 1, 0, "Actions")

        engine.rootContext().setContextProperty("ExtruderManager", ExtruderManager.getInstance())

        for path in Resources.getAllResourcesOfType(CuraApplication.ResourceTypes.QmlFiles):
            type_name = os.path.splitext(os.path.basename(path))[0]
            if type_name in ("Cura", "Actions"):
                continue

            # Ignore anything that is not a QML file.
            if not path.endswith(".qml"):
                continue

            qmlRegisterType(QUrl.fromLocalFile(path), "Cura", 1, 0, type_name)

    def onSelectionChanged(self):
        if Selection.hasSelection():
            if self.getController().getActiveTool():
                # If the tool has been disabled by the new selection
                if not self.getController().getActiveTool().getEnabled():
                    # Default
                    self.getController().setActiveTool("TranslateTool")
            else:
                if abs(time.time() - self._previous_active_tool_time) > 0.5:
                    self._previous_active_tool = None
                if self._previous_active_tool:
                    self.getController().setActiveTool(self._previous_active_tool)
                    if not self.getController().getActiveTool().getEnabled():
                        self.getController().setActiveTool("TranslateTool")
                    self._previous_active_tool = None
                else:
                    # Default
                    self.getController().setActiveTool("TranslateTool")

            if Preferences.getInstance().getValue("view/center_on_select"):
                self._center_after_select = True
        else:
            if self.getController().getActiveTool():
                self._previous_active_tool = self.getController().getActiveTool().getPluginId()
                self.getController().setActiveTool(None)
                self._previous_active_tool_time = time.time()

    def _onToolOperationStopped(self, event):
        if self._center_after_select and Selection.getSelectedObject(0) is not None:
            self._center_after_select = False
            self._camera_animation.setStart(self.getController().getTool("CameraTool").getOrigin())
            self._camera_animation.setTarget(Selection.getSelectedObject(0).getWorldPosition())
            self._camera_animation.start()

    def _onGlobalContainerChanged(self):
        if self._global_container_stack is not None:
            machine_file_formats = [file_type.strip() for file_type in self._global_container_stack.getMetaDataEntry("file_formats").split(";")]
            new_preferred_mimetype = ""
            if machine_file_formats:
                new_preferred_mimetype =  machine_file_formats[0]

            if new_preferred_mimetype != self._preferred_mimetype:
                self._preferred_mimetype = new_preferred_mimetype
                self.preferredOutputMimetypeChanged.emit()

    requestAddPrinter = pyqtSignal()
    activityChanged = pyqtSignal()
    sceneBoundingBoxChanged = pyqtSignal()
    preferredOutputMimetypeChanged = pyqtSignal()

    @pyqtProperty(bool, notify = activityChanged)
    def platformActivity(self):
        return self._platform_activity

    @pyqtProperty(str, notify=preferredOutputMimetypeChanged)
    def preferredOutputMimetype(self):
        return self._preferred_mimetype

    @pyqtProperty(str, notify = sceneBoundingBoxChanged)
    def getSceneBoundingBoxString(self):
        return self._i18n_catalog.i18nc("@info 'width', 'depth' and 'height' are variable names that must NOT be translated; just translate the format of ##x##x## mm.", "%(width).1f x %(depth).1f x %(height).1f mm") % {'width' : self._scene_bounding_box.width.item(), 'depth': self._scene_bounding_box.depth.item(), 'height' : self._scene_bounding_box.height.item()}

    def updatePlatformActivity(self, node = None):
        count = 0
        scene_bounding_box = None
        is_block_slicing_node = False
        for node in DepthFirstIterator(self.getController().getScene().getRoot()):
            if type(node) is not SceneNode or (not node.getMeshData() and not node.callDecoration("getLayerData")):
                continue
            if node.callDecoration("isBlockSlicing"):
                is_block_slicing_node = True
            if node.callDecoration("hasPrintStatistics"):
                amounts = node.callDecoration("getMaterialAmounts")
                if type(amounts) is not list:
                    amounts = [0]
                self.getBackend().printDurationMessage.emit(node.callDecoration("getPrintTime"), amounts)

            count += 1
            if not scene_bounding_box:
                scene_bounding_box = node.getBoundingBox()
            else:
                other_bb = node.getBoundingBox()
                if other_bb is not None:
                    scene_bounding_box = scene_bounding_box + node.getBoundingBox()

        print_information = self.getPrintInformation()
        if print_information:
            print_information.setPreSliced(is_block_slicing_node)


        if not scene_bounding_box:
            scene_bounding_box = AxisAlignedBox.Null

        if repr(self._scene_bounding_box) != repr(scene_bounding_box) and scene_bounding_box.isValid():
            self._scene_bounding_box = scene_bounding_box
            self.sceneBoundingBoxChanged.emit()

        self._platform_activity = True if count > 0 else False
        self.activityChanged.emit()

    # Remove all selected objects from the scene.
    @pyqtSlot()
    @deprecated("Moved to CuraActions", "2.6")
    def deleteSelection(self):
        if not self.getController().getToolsEnabled():
            return
        removed_group_nodes = []
        op = GroupedOperation()
        nodes = Selection.getAllSelectedObjects()
        for node in nodes:
            op.addOperation(RemoveSceneNodeOperation(node))
            group_node = node.getParent()
            if group_node and group_node.callDecoration("isGroup") and group_node not in removed_group_nodes:
                remaining_nodes_in_group = list(set(group_node.getChildren()) - set(nodes))
                if len(remaining_nodes_in_group) == 1:
                    removed_group_nodes.append(group_node)
                    op.addOperation(SetParentOperation(remaining_nodes_in_group[0], group_node.getParent()))
                    op.addOperation(RemoveSceneNodeOperation(group_node))
        op.push()

    ##  Remove an object from the scene.
    #   Note that this only removes an object if it is selected.
    @pyqtSlot("quint64")
    @deprecated("Use deleteSelection instead", "2.6")
    def deleteObject(self, object_id):
        if not self.getController().getToolsEnabled():
            return

        node = self.getController().getScene().findObject(object_id)

        if not node and object_id != 0:  # Workaround for tool handles overlapping the selected object
            node = Selection.getSelectedObject(0)

        if node:
            op = GroupedOperation()
            op.addOperation(RemoveSceneNodeOperation(node))

            group_node = node.getParent()
            if group_node:
                # Note that at this point the node has not yet been deleted
                if len(group_node.getChildren()) <= 2 and group_node.callDecoration("isGroup"):
                    op.addOperation(SetParentOperation(group_node.getChildren()[0], group_node.getParent()))
                    op.addOperation(RemoveSceneNodeOperation(group_node))

            op.push()

    ##  Create a number of copies of existing object.
    #   \param object_id
    #   \param count number of copies
    #   \param min_offset minimum offset to other objects.
    @pyqtSlot("quint64", int)
    @deprecated("Use CuraActions::multiplySelection", "2.6")
    def multiplyObject(self, object_id, count, min_offset = 8):
        node = self.getController().getScene().findObject(object_id)
        if not node:
            node = Selection.getSelectedObject(0)

        while node.getParent() and node.getParent().callDecoration("isGroup"):
            node = node.getParent()

        job = MultiplyObjectsJob([node], count, min_offset)
        job.start()
        return

    ##  Center object on platform.
    @pyqtSlot("quint64")
    @deprecated("Use CuraActions::centerSelection", "2.6")
    def centerObject(self, object_id):
        node = self.getController().getScene().findObject(object_id)
        if not node and object_id != 0:  # Workaround for tool handles overlapping the selected object
            node = Selection.getSelectedObject(0)

        if not node:
            return

        if node.getParent() and node.getParent().callDecoration("isGroup"):
            node = node.getParent()

        if node:
            op = SetTransformOperation(node, Vector())
            op.push()

    ##  Select all nodes containing mesh data in the scene.
    @pyqtSlot()
    def selectAll(self):
        if not self.getController().getToolsEnabled():
            return

        Selection.clear()
        for node in DepthFirstIterator(self.getController().getScene().getRoot()):
            if type(node) is not SceneNode:
                continue
            if not node.getMeshData() and not node.callDecoration("isGroup"):
                continue  # Node that doesnt have a mesh and is not a group.
            if node.getParent() and node.getParent().callDecoration("isGroup"):
                continue  # Grouped nodes don't need resetting as their parent (the group) is resetted)
            if not node.isSelectable():
                continue  # i.e. node with layer data
            Selection.add(node)

    ##  Delete all nodes containing mesh data in the scene.
    @pyqtSlot()
    def deleteAll(self):
        Logger.log("i", "Clearing scene")
        if not self.getController().getToolsEnabled():
            return

        nodes = []
        for node in DepthFirstIterator(self.getController().getScene().getRoot()):
            if type(node) is not SceneNode:
                continue
            if (not node.getMeshData() and not node.callDecoration("getLayerData")) and not node.callDecoration("isGroup"):
                continue  # Node that doesnt have a mesh and is not a group.
            if node.getParent() and node.getParent().callDecoration("isGroup"):
                continue  # Grouped nodes don't need resetting as their parent (the group) is resetted)
            nodes.append(node)
        if nodes:
            op = GroupedOperation()

            for node in nodes:
                op.addOperation(RemoveSceneNodeOperation(node))

            op.push()
            Selection.clear()

    ## Reset all translation on nodes with mesh data. 
    @pyqtSlot()
    def resetAllTranslation(self):
        Logger.log("i", "Resetting all scene translations")
        nodes = []
        for node in DepthFirstIterator(self.getController().getScene().getRoot()):
            if type(node) is not SceneNode:
                continue
            if not node.getMeshData() and not node.callDecoration("isGroup"):
                continue  # Node that doesnt have a mesh and is not a group.
            if node.getParent() and node.getParent().callDecoration("isGroup"):
                continue  # Grouped nodes don't need resetting as their parent (the group) is resetted)
            if not node.isSelectable():
                continue  # i.e. node with layer data
            nodes.append(node)

        if nodes:
            op = GroupedOperation()
            for node in nodes:
                # Ensure that the object is above the build platform
                node.removeDecorator(ZOffsetDecorator.ZOffsetDecorator)
                if node.getBoundingBox():
                    center_y = node.getWorldPosition().y - node.getBoundingBox().bottom
                else:
                    center_y = 0
                op.addOperation(SetTransformOperation(node, Vector(0, center_y, 0)))
            op.push()

    ## Reset all transformations on nodes with mesh data.
    @pyqtSlot()
    def resetAll(self):
        Logger.log("i", "Resetting all scene transformations")
        nodes = []
        for node in DepthFirstIterator(self.getController().getScene().getRoot()):
            if type(node) is not SceneNode:
                continue
            if not node.getMeshData() and not node.callDecoration("isGroup"):
                continue  # Node that doesnt have a mesh and is not a group.
            if node.getParent() and node.getParent().callDecoration("isGroup"):
                continue  # Grouped nodes don't need resetting as their parent (the group) is resetted)
            if not node.isSelectable():
                continue  # i.e. node with layer data
            nodes.append(node)

        if nodes:
            op = GroupedOperation()
            for node in nodes:
                # Ensure that the object is above the build platform
                node.removeDecorator(ZOffsetDecorator.ZOffsetDecorator)
                if node.getBoundingBox():
                    center_y = node.getWorldPosition().y - node.getBoundingBox().bottom
                else:
                    center_y = 0
                op.addOperation(SetTransformOperation(node, Vector(0, center_y, 0), Quaternion(), Vector(1, 1, 1)))
            op.push()

    ##  Arrange all objects.
    @pyqtSlot()
    def arrangeAll(self):
        nodes = []
        for node in DepthFirstIterator(self.getController().getScene().getRoot()):
            if type(node) is not SceneNode:
                continue
            if not node.getMeshData() and not node.callDecoration("isGroup"):
                continue  # Node that doesnt have a mesh and is not a group.
            if node.getParent() and node.getParent().callDecoration("isGroup"):
                continue  # Grouped nodes don't need resetting as their parent (the group) is resetted)
            if not node.isSelectable():
                continue  # i.e. node with layer data
            # Skip nodes that are too big
            if node.getBoundingBox().width < self._volume.getBoundingBox().width or node.getBoundingBox().depth < self._volume.getBoundingBox().depth:
                nodes.append(node)
        self.arrange(nodes, fixed_nodes = [])

    ##  Arrange Selection
    @pyqtSlot()
    def arrangeSelection(self):
        nodes = Selection.getAllSelectedObjects()

        # What nodes are on the build plate and are not being moved
        fixed_nodes = []
        for node in DepthFirstIterator(self.getController().getScene().getRoot()):
            if type(node) is not SceneNode:
                continue
            if not node.getMeshData() and not node.callDecoration("isGroup"):
                continue  # Node that doesnt have a mesh and is not a group.
            if node.getParent() and node.getParent().callDecoration("isGroup"):
                continue  # Grouped nodes don't need resetting as their parent (the group) is resetted)
            if not node.isSelectable():
                continue  # i.e. node with layer data
            if node in nodes:  # exclude selected node from fixed_nodes
                continue
            fixed_nodes.append(node)
        self.arrange(nodes, fixed_nodes)

    ##  Arrange a set of nodes given a set of fixed nodes
    #   \param nodes nodes that we have to place
    #   \param fixed_nodes nodes that are placed in the arranger before finding spots for nodes
    def arrange(self, nodes, fixed_nodes):
        job = ArrangeObjectsJob(nodes, fixed_nodes)
        job.start()

    ##  Reload all mesh data on the screen from file.
    @pyqtSlot()
    def reloadAll(self):
        Logger.log("i", "Reloading all loaded mesh data.")
        nodes = []
        for node in DepthFirstIterator(self.getController().getScene().getRoot()):
            if type(node) is not SceneNode or not node.getMeshData():
                continue

            nodes.append(node)

        if not nodes:
            return

        for node in nodes:
            file_name = node.getMeshData().getFileName()
            if file_name:
                job = ReadMeshJob(file_name)
                job._node = node
                job.finished.connect(self._reloadMeshFinished)
                job.start()
            else:
                Logger.log("w", "Unable to reload data because we don't have a filename.")
    
    ##  Get logging data of the backend engine
    #   \returns \type{string} Logging data
    @pyqtSlot(result = str)
    def getEngineLog(self):
        log = ""

        for entry in self.getBackend().getLog():
            log += entry.decode()

        return log

    @pyqtSlot("QStringList")
    def setExpandedCategories(self, categories):
        categories = list(set(categories))
        categories.sort()
        joined = ";".join(categories)
        if joined != Preferences.getInstance().getValue("cura/categories_expanded"):
            Preferences.getInstance().setValue("cura/categories_expanded", joined)
            self.expandedCategoriesChanged.emit()

    expandedCategoriesChanged = pyqtSignal()

    @pyqtProperty("QStringList", notify = expandedCategoriesChanged)
    def expandedCategories(self):
        return Preferences.getInstance().getValue("cura/categories_expanded").split(";")

    @pyqtSlot()
    def mergeSelected(self):
        self.groupSelected()
        try:
            group_node = Selection.getAllSelectedObjects()[0]
        except Exception as e:
            Logger.log("d", "mergeSelected: Exception:", e)
            return

        meshes = [node.getMeshData() for node in group_node.getAllChildren() if node.getMeshData()]

        # Compute the center of the objects
        object_centers = []
        # Forget about the translation that the original objects have
        zero_translation = Matrix(data=numpy.zeros(3))
        for mesh, node in zip(meshes, group_node.getChildren()):
            transformation = node.getLocalTransformation()
            transformation.setTranslation(zero_translation)
            transformed_mesh = mesh.getTransformed(transformation)
            center = transformed_mesh.getCenterPosition()
            if center is not None:
                object_centers.append(center)
        if object_centers and len(object_centers) > 0:
            middle_x = sum([v.x for v in object_centers]) / len(object_centers)
            middle_y = sum([v.y for v in object_centers]) / len(object_centers)
            middle_z = sum([v.z for v in object_centers]) / len(object_centers)
            offset = Vector(middle_x, middle_y, middle_z)
        else:
            offset = Vector(0, 0, 0)

        # Move each node to the same position.
        for mesh, node in zip(meshes, group_node.getChildren()):
            transformation = node.getLocalTransformation()
            transformation.setTranslation(zero_translation)
            transformed_mesh = mesh.getTransformed(transformation)

            # Align the object around its zero position
            # and also apply the offset to center it inside the group.
            node.setPosition(-transformed_mesh.getZeroPosition() - offset)

        # Use the previously found center of the group bounding box as the new location of the group
        group_node.setPosition(group_node.getBoundingBox().center)

    @pyqtSlot()
    def groupSelected(self):
        # Create a group-node
        group_node = SceneNode()
        group_decorator = GroupDecorator()
        group_node.addDecorator(group_decorator)
        group_node.addDecorator(ConvexHullDecorator())
        group_node.setParent(self.getController().getScene().getRoot())
        group_node.setSelectable(True)
        center = Selection.getSelectionCenter()
        group_node.setPosition(center)
        group_node.setCenterPosition(center)

        # Move selected nodes into the group-node
        Selection.applyOperation(SetParentOperation, group_node)

        # Deselect individual nodes and select the group-node instead
        for node in group_node.getChildren():
            Selection.remove(node)
        Selection.add(group_node)

    @pyqtSlot()
    def ungroupSelected(self):
        selected_objects = Selection.getAllSelectedObjects().copy()
        for node in selected_objects:
            if node.callDecoration("isGroup"):
                op = GroupedOperation()

                group_parent = node.getParent()
                children = node.getChildren().copy()
                for child in children:
                    # Set the parent of the children to the parent of the group-node
                    op.addOperation(SetParentOperation(child, group_parent))

                    # Add all individual nodes to the selection
                    Selection.add(child)

                op.push()
                # Note: The group removes itself from the scene once all its children have left it,
                # see GroupDecorator._onChildrenChanged

    def _createSplashScreen(self):
        run_headless = self.getCommandLineOption("headless", False)
        if run_headless:
            return None
        return CuraSplashScreen.CuraSplashScreen()

    def _onActiveMachineChanged(self):
        pass

    fileLoaded = pyqtSignal(str)

    def _onFileLoaded(self, job):
        nodes = job.getResult()
        for node in nodes:
            if node is not None:
                self.fileLoaded.emit(job.getFileName())
                node.setSelectable(True)
                node.setName(os.path.basename(job.getFileName()))
                op = AddSceneNodeOperation(node, self.getController().getScene().getRoot())
                op.push()

                self.getController().getScene().sceneChanged.emit(node) #Force scene change.

    def _onJobFinished(self, job):
        if type(job) is not ReadMeshJob or not job.getResult():
            return

        f = QUrl.fromLocalFile(job.getFileName())
        if f in self._recent_files:
            self._recent_files.remove(f)

        self._recent_files.insert(0, f)
        if len(self._recent_files) > 10:
            del self._recent_files[10]

        pref = ""
        for path in self._recent_files:
            pref += path.toLocalFile() + ";"

        Preferences.getInstance().setValue("cura/recent_files", pref)
        self.recentFilesChanged.emit()

    def _reloadMeshFinished(self, job):
        # TODO; This needs to be fixed properly. We now make the assumption that we only load a single mesh!
        mesh_data = job.getResult()[0].getMeshData()
        if mesh_data:
            job._node.setMeshData(mesh_data)
        else:
            Logger.log("w", "Could not find a mesh in reloaded node.")

    def _openFile(self, filename):
        self.readLocalFile(QUrl.fromLocalFile(filename))

    def _addProfileReader(self, profile_reader):
        # TODO: Add the profile reader to the list of plug-ins that can be used when importing profiles.
        pass

    def _addProfileWriter(self, profile_writer):
        pass

    @pyqtSlot("QSize")
    def setMinimumWindowSize(self, size):
        self.getMainWindow().setMinimumSize(size)

    def getBuildVolume(self):
        return self._volume

    additionalComponentsChanged = pyqtSignal(str, arguments = ["areaId"])

    @pyqtProperty("QVariantMap", notify = additionalComponentsChanged)
    def additionalComponents(self):
        return self._additional_components

    ##  Add a component to a list of components to be reparented to another area in the GUI.
    #   The actual reparenting is done by the area itself.
    #   \param area_id \type{str} Identifying name of the area to which the component should be reparented
    #   \param component \type{QQuickComponent} The component that should be reparented
    @pyqtSlot(str, "QVariant")
    def addAdditionalComponent(self, area_id, component):
        if area_id not in self._additional_components:
            self._additional_components[area_id] = []
        self._additional_components[area_id].append(component)

        self.additionalComponentsChanged.emit(area_id)

    @pyqtSlot(str)
    def log(self, msg):
        Logger.log("d", msg)

    @pyqtSlot(QUrl)
    def readLocalFile(self, file):
        if not file.isValid():
            return

        scene = self.getController().getScene()

        for node in DepthFirstIterator(scene.getRoot()):
            if node.callDecoration("isBlockSlicing"):
                self.deleteAll()
                break

        f = file.toLocalFile()
        extension = os.path.splitext(f)[1]
        filename = os.path.basename(f)
        if len(self._currently_loading_files) > 0:
            # If a non-slicable file is already being loaded, we prevent loading of any further non-slicable files
            if extension.lower() in self._non_sliceable_extensions:
                message = Message(
                    self._i18n_catalog.i18nc("@info:status",
                                       "Only one G-code file can be loaded at a time. Skipped importing {0}",
                                       filename), title = self._i18n_catalog.i18nc("@info:title", "Warning"))
                message.show()
                return
            # If file being loaded is non-slicable file, then prevent loading of any other files
            extension = os.path.splitext(self._currently_loading_files[0])[1]
            if extension.lower() in self._non_sliceable_extensions:
                message = Message(
                    self._i18n_catalog.i18nc("@info:status",
                                       "Can't open any other file if G-code is loading. Skipped importing {0}",
                                       filename), title = self._i18n_catalog.i18nc("@info:title", "Error"))
                message.show()
                return

        self._currently_loading_files.append(f)
        if extension in self._non_sliceable_extensions:
            self.deleteAll()

        job = ReadMeshJob(f)
        job.finished.connect(self._readMeshFinished)
        job.start()

    def _readMeshFinished(self, job):
        nodes = job.getResult()
        filename = job.getFileName()
        self._currently_loading_files.remove(filename)

        root = self.getController().getScene().getRoot()
        arranger = Arrange.create(scene_root = root)
        min_offset = 8

        self.fileLoaded.emit(filename)

        for node in nodes:
            node.setSelectable(True)
            node.setName(os.path.basename(filename))

            extension = os.path.splitext(filename)[1]
            if extension.lower() in self._non_sliceable_extensions:
                self.getController().setActiveView("LayerView")
                view = self.getController().getActiveView()
                view.resetLayerData()
                view.setLayer(9999999)
                view.calculateMaxLayers()

                block_slicing_decorator = BlockSlicingDecorator()
                node.addDecorator(block_slicing_decorator)
            else:
                sliceable_decorator = SliceableObjectDecorator()
                node.addDecorator(sliceable_decorator)

            scene = self.getController().getScene()

            # If there is no convex hull for the node, start calculating it and continue.
            if not node.getDecorator(ConvexHullDecorator):
                node.addDecorator(ConvexHullDecorator())
            for child in node.getAllChildren():
                if not child.getDecorator(ConvexHullDecorator):
                    child.addDecorator(ConvexHullDecorator())

            if node.callDecoration("isSliceable"):
                # Only check position if it's not already blatantly obvious that it won't fit.
                if node.getBoundingBox().width < self._volume.getBoundingBox().width or node.getBoundingBox().depth < self._volume.getBoundingBox().depth:
                    # Find node location
                    offset_shape_arr, hull_shape_arr = ShapeArray.fromNode(node, min_offset = min_offset)

                    # If a model is to small then it will not contain any points
                    if offset_shape_arr is None and hull_shape_arr is None:
                        Message(self._i18n_catalog.i18nc("@info:status", "The selected model was too small to load."),
                                title=self._i18n_catalog.i18nc("@info:title", "Warning")).show()
                        return

                    # Step is for skipping tests to make it a lot faster. it also makes the outcome somewhat rougher
                    node, _ = arranger.findNodePlacement(node, offset_shape_arr, hull_shape_arr, step = 10)

            op = AddSceneNodeOperation(node, scene.getRoot())
            op.push()
            scene.sceneChanged.emit(node)

    def addNonSliceableExtension(self, extension):
        self._non_sliceable_extensions.append(extension)

    ##  Display text on the splash screen.
    def showSplashMessage(self, message):
        splash = self.getSplashScreen()
        if splash:
            # splash.setText(message)
            self.processEvents()

    @pyqtSlot(str, result=bool)
    def checkIsValidProjectFile(self, file_url):
        """
        Checks if the given file URL is a valid project file.
        """
        try:
            file_path = QUrl(file_url).toLocalFile()
            workspace_reader = self.getWorkspaceFileHandler().getReaderForFile(file_path)
            if workspace_reader is None:
                return False  # non-project files won't get a reader

            result = workspace_reader.preRead(file_path, show_dialog=False)
            return result == WorkspaceReader.PreReadResult.accepted
        except Exception as e:
            Logger.log("e", "Could not check file %s: %s", file_url, e)
            return False

    def _onContextMenuRequested(self, x: float, y: float) -> None:
        # Ensure we select the object if we request a context menu over an object without having a selection.
        if not Selection.hasSelection():
            node = self.getController().getScene().findObject(self.getRenderer().getRenderPass("selection").getIdAtPosition(x, y))
            if node:
                while(node.getParent() and node.getParent().callDecoration("isGroup")):
                    node = node.getParent()

                Selection.add(node)
