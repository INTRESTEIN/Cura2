# Copyright (c) 2017 Aleph Objects, Inc.
# Cura is released under the terms of the AGPLv3 or higher.

from UM.Application import Application
from UM.Extension import Extension
from UM.Scene.Plane import Plane
from UM.i18n import i18nCatalog
from UM.Operations.AddSceneNodeOperation import AddSceneNodeOperation
from UM.Scene.Selection import Selection
from UM.Logger import Logger
from UM.Scene.SceneNode import SceneNode
from UM.Operations.GroupedOperation import GroupedOperation
from UM.Operations.RemoveSceneNodeOperation import RemoveSceneNodeOperation
from UM.Mesh.MeshBuilder import MeshBuilder
import numpy
import math

i18n_catalog = i18nCatalog("ModelSubdividerPlugin")


class IntersectionType:
    Point = 0
    Segment = 1
    Face = 2


class ModelSubdividerPlugin(Extension):
    def __init__(self):
        super().__init__()
        self.addMenuItem(i18n_catalog.i18n("Create plane"), self.createPlane)
        self.addMenuItem(i18n_catalog.i18n("Subdivide mesh by plane"), self.subdivide)

    def createPlane(self):
        plane = Plane()
        scene = Application.getInstance().getController().getScene()
        operation = AddSceneNodeOperation(plane, scene.getRoot())
        operation.push()

    def subdivide(self):
        if Selection.getCount() != 2:
            Logger.log("w", i18n_catalog.i18n("Cannot subdivide: objects != 2"))
            return
        object1 = Selection.getSelectedObject(0)
        object2 = Selection.getSelectedObject(1)
        if type(object1) is SceneNode and type(object2) is Plane:
            obj = object1
            plane = object2
        elif type(object2) is SceneNode and type(object1) is Plane:
            obj = object2
            plane = object1
        else:
            Logger.log("w", i18n_catalog.i18n("Cannot subdivide: object and plane need to be selected"))
            return

        result = self._subdivide(obj, plane)
        if type(result) is tuple:
            operation = GroupedOperation()
            operation.addOperation(RemoveSceneNodeOperation(plane))
            operation.addOperation(RemoveSceneNodeOperation(obj))
            operation.addOperation(AddSceneNodeOperation(result[0], obj.getParent()))
            if len(result) == 2:
                operation.addOperation(AddSceneNodeOperation(result[1], obj.getParent()))
            operation.push()
        else:
            Logger.log("w", i18n_catalog.i18n("Cannot subdivide"))

    def _subdivide(self, mesh, plane):
        plane_mesh_data = plane.getMeshData()
        plane_vertices = plane_mesh_data.getVertices()
        plane_face = [plane_vertices[0], plane_vertices[1], plane_vertices[2]]
        builders = [MeshBuilder(), MeshBuilder()]
        mesh_data = mesh.getMeshData()
        vertices = mesh_data.getVertices()
        indices = mesh_data.getIndices()
        faces = []
        for index_array in indices:
            faces.append([vertices[index_array[0]], vertices[index_array[1]], vertices[index_array[2]]])
        intersected_faces = []
        for f in faces:
            intersection_type = self.check_intersection_with_triangle(plane_face, f)
            if (intersection_type is not None and intersection_type[0] == IntersectionType.Point) \
                    or intersection_type is None:
                side = self.check_plane_side(plane_face, f)
                self.add_face_to_builder(builders[side], f)
            else:
                intersected_faces.append([f, intersection_type])
        for f in intersected_faces:
            if f[1][0] == IntersectionType.Face:
                self.add_face_to_builder(builders[0], f[0])
                self.add_face_to_builder(builders[1], f[0])
            elif f[1][0] == IntersectionType.Segment:
                self.add_face_to_builder(builders[0], f[0])
                self.add_face_to_builder(builders[1], f[0])
        nodes = [SceneNode(), SceneNode()]
        for n in range(len(nodes)):
            nodes[n].setMeshData(builders[n].build())
            nodes[n].setSelectable(True)
            nodes[n].setScale(mesh.getScale())
        return nodes[0], nodes[1]

    def add_face_to_builder(self, builder, face):
        builder.addFaceByPoints(face[0][0], face[0][1], face[0][2],
                                face[1][0], face[1][1], face[1][2],
                                face[2][0], face[2][1], face[2][2])

    def check_plane_side(self, plane_face, face):
        n = numpy.cross(plane_face[1] - plane_face[0], plane_face[2] - plane_face[0])
        v = plane_face[0] - face[0]
        d = numpy.inner(n, v)
        if d > 0:
            return 0
        else:
            return 1

    def check_intersection_with_triangle(self, plane_face, face):
        intersection_points = []
        for i in range(3):
            i2 = i + 1 if i < 2 else 0
            segment = [face[i], face[i2]]
            point = self.check_intersection_with_segment(plane_face, segment)
            if point is not None:
                intersection_points.append(point)
        if len(intersection_points) == 1:
            return IntersectionType.Point, intersection_points[0]
        elif len(intersection_points) == 2:
            return IntersectionType.Segment, intersection_points
        elif len(intersection_points) == 3:
            return IntersectionType.Face, face
        return None

    def check_intersection_with_segment(self, plane_face, segment):
        epsilon = 1e-4
        n = numpy.cross(plane_face[1] - plane_face[0], plane_face[2] - plane_face[0])
        v = plane_face[0] - segment[0]
        d = numpy.inner(n, v)
        w = segment[1] - segment[0]
        e = numpy.inner(n, w)
        if math.fabs(e) > epsilon:
            o = segment[0] + w * d / e
            if numpy.inner(segment[0] - o, segment[1] - o) <= 0:
                return o
        return None
