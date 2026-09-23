"""Upload YUV frames as textures and draw them with a colour-converting shader.

Everything here must run with a current OpenGL context. The renderer is
deliberately independent of the widget so the same code can draw into a
``QOpenGLWidget`` or into an offscreen framebuffer (used by the self-check).
"""

from __future__ import annotations

import logging
import struct

from PySide6.QtCore import QSize
from PySide6.QtGui import QOpenGLContext, QSurfaceFormat, QVector3D
from PySide6.QtOpenGL import (
    QOpenGLBuffer,
    QOpenGLPixelTransferOptions,
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLTexture,
    QOpenGLVersionFunctionsFactory,
    QOpenGLVersionProfile,
    QOpenGLVertexArrayObject,
)

from ..errors import MirrorScreenError
from ..video.frame import VideoFrame
from .color import ColorConversion
from .geometry import Rect, quad_transform

log = logging.getLogger(__name__)

# Raw GL constants, so we do not need PyOpenGL.
GL_FLOAT = 0x1406
GL_TRIANGLE_STRIP = 0x0005
GL_COLOR_BUFFER_BIT = 0x4000
GL_TEXTURE0 = 0x84C0

_GL_TEXTURE0 = GL_TEXTURE0

_VERTEX_SHADER = """#version 330 core
layout(location = 0) in vec2 aPos;
layout(location = 1) in vec2 aTex;
uniform vec4 uRect;      // xy = scale, zw = offset, in clip space
out vec2 vTex;
void main() {
    vTex = aTex;
    gl_Position = vec4(aPos * uRect.xy + uRect.zw, 0.0, 1.0);
}
"""

_FRAGMENT_SHADER = """#version 330 core
in vec2 vTex;
out vec4 fragColor;
uniform sampler2D uPlaneY;
uniform sampler2D uPlaneU;
uniform sampler2D uPlaneV;
uniform vec3 uCol0;
uniform vec3 uCol1;
uniform vec3 uCol2;
uniform vec3 uOffset;
void main() {
    vec3 yuv = vec3(
        texture(uPlaneY, vTex).r,
        texture(uPlaneU, vTex).r,
        texture(uPlaneV, vTex).r
    );
    vec3 scaled = yuv - uOffset;
    mat3 conversion = mat3(uCol0, uCol1, uCol2);
    fragColor = vec4(clamp(conversion * scaled, 0.0, 1.0), 1.0);
}
"""


def current_gl_functions(context: QOpenGLContext | None = None):
    """Return the OpenGL 3.3 core function table for the current context."""
    context = context or QOpenGLContext.currentContext()
    if context is None:
        raise MirrorScreenError("no current OpenGL context")

    for version in ((3, 3), (4, 1), (4, 0), (3, 2)):
        profile = QOpenGLVersionProfile()
        profile.setVersion(*version)
        profile.setProfile(
            QSurfaceFormat.OpenGLContextProfile.CoreProfile
            if version >= (3, 2)
            else QSurfaceFormat.OpenGLContextProfile.NoProfile
        )
        functions = QOpenGLVersionFunctionsFactory.get(profile, context)
        if functions is not None:
            return functions

    raise MirrorScreenError(
        "could not obtain OpenGL 3.3 core functions; the driver may be too old"
    )


class YuvQuadRenderer:
    """Draws a :class:`VideoFrame` as a quad, converting YUV to RGB on the GPU."""

    def __init__(self, *, filter_mode: str = "linear") -> None:
        self.filter_mode = filter_mode
        self._program: QOpenGLShaderProgram | None = None
        self._vao: QOpenGLVertexArrayObject | None = None
        self._vbo: QOpenGLBuffer | None = None
        self._textures: list[QOpenGLTexture] = []
        self._texture_size: tuple[int, int] | None = None
        self._uploaded_frame: VideoFrame | None = None
        self._transfer_options = QOpenGLPixelTransferOptions()
        self._transfer_options.setAlignment(1)
        self.gl = None

    # -- setup --------------------------------------------------------------
    def initialize(self) -> None:
        """Create the shader program and buffers (needs a current context)."""
        self.gl = current_gl_functions()

        program = QOpenGLShaderProgram()
        if not program.addShaderFromSourceCode(
            QOpenGLShader.ShaderTypeBit.Vertex, _VERTEX_SHADER
        ):
            raise MirrorScreenError(f"vertex shader failed: {program.log()}")
        if not program.addShaderFromSourceCode(
            QOpenGLShader.ShaderTypeBit.Fragment, _FRAGMENT_SHADER
        ):
            raise MirrorScreenError(f"fragment shader failed: {program.log()}")
        if not program.link():
            raise MirrorScreenError(f"shader link failed: {program.log()}")
        self._program = program

        self._vao = QOpenGLVertexArrayObject()
        if not self._vao.create():
            raise MirrorScreenError("could not create a vertex array object")
        self._vao.bind()

        self._vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        self._vbo.create()
        self._vbo.bind()

        # A unit quad centred on the origin. The texture coordinates are
        # vertically flipped because OpenGL's v axis points up while the frame
        # rows start at the top of the image.
        corners = (
            (-0.5, -0.5, 0.0, 1.0),  # bottom-left  -> image bottom
            (0.5, -0.5, 1.0, 1.0),  # bottom-right
            (-0.5, 0.5, 0.0, 0.0),  # top-left     -> image top
            (0.5, 0.5, 1.0, 0.0),  # top-right
        )
        data = struct.pack("<16f", *(value for corner in corners for value in corner))
        self._vbo.allocate(data, len(data))

        program.bind()
        program.enableAttributeArray(0)
        program.setAttributeBuffer(0, GL_FLOAT, 0, 2, 4 * 4)
        program.enableAttributeArray(1)
        program.setAttributeBuffer(1, GL_FLOAT, 2 * 4, 2, 4 * 4)
        self._vbo.release()
        self._vao.release()
        program.release()

    @property
    def is_initialized(self) -> bool:
        return self._program is not None

    def release(self) -> None:
        """Destroy GPU resources (needs a current context)."""
        for texture in self._textures:
            texture.destroy()
        self._textures = []
        self._texture_size = None
        self._uploaded_frame = None

        if self._vbo is not None:
            self._vbo.destroy()
            self._vbo = None
        if self._vao is not None:
            self._vao.destroy()
            self._vao = None
        if self._program is not None:
            # Detach the shaders explicitly; the program object itself is
            # released when Python drops the last reference.
            self._program.removeAllShaders()
            self._program = None

    def set_filter_mode(self, mode: str) -> None:
        if mode == self.filter_mode:
            return
        self.filter_mode = mode
        for texture in self._textures:
            self._apply_filter(texture)

    # -- drawing ------------------------------------------------------------
    def clear(self, red: float = 0.06, green: float = 0.06, blue: float = 0.07) -> None:
        if self.gl is None:
            return
        self.gl.glClearColor(red, green, blue, 1.0)
        self.gl.glClear(GL_COLOR_BUFFER_BIT)

    def set_viewport(self, width: int, height: int) -> None:
        if self.gl is None:
            return
        self.gl.glViewport(0, 0, max(1, int(width)), max(1, int(height)))

    def upload(self, frame: VideoFrame) -> None:
        """Upload the frame's planes, reallocating textures if the size changed."""
        if self._uploaded_frame is frame:
            return

        if self._texture_size != frame.size:
            self._recreate_textures(frame)
            self._texture_size = frame.size

        planes = (frame.y, frame.u, frame.v)
        for index, (texture, plane) in enumerate(zip(self._textures, planes, strict=True)):
            self.gl.glActiveTexture(_GL_TEXTURE0 + index)
            texture.bind(index)
            texture.setData(
                QOpenGLTexture.PixelFormat.Red,
                QOpenGLTexture.PixelType.UInt8,
                plane.tobytes(),
                self._transfer_options,
            )

        self._uploaded_frame = frame

    def render(
        self,
        frame: VideoFrame,
        layout: Rect,
        view_size: QSize | tuple[int, int],
        conversion: ColorConversion,
    ) -> None:
        """Draw ``frame`` into ``layout`` (widget pixels) inside the viewport."""
        if self._program is None:
            raise MirrorScreenError("renderer used before initialize()")

        self.upload(frame)

        view_width, view_height = view_size
        if view_width <= 0 or view_height <= 0:
            return

        columns = conversion.columns
        program = self._program
        program.bind()

        # Map the unit quad onto ``layout`` in clip space. Clip space has y up
        # while ``layout`` is measured in pixels from the top.
        # quad_transform keeps scale_y positive: a negative one mirrors the quad
        # and shows the whole picture upside down.
        program.setUniformValue(
            "uRect", *quad_transform(layout, int(view_width), int(view_height))
        )
        program.setUniformValue("uCol0", QVector3D(*columns[0]))
        program.setUniformValue("uCol1", QVector3D(*columns[1]))
        program.setUniformValue("uCol2", QVector3D(*columns[2]))
        program.setUniformValue("uOffset", QVector3D(*conversion.offset))

        for index, (texture, uniform) in enumerate(
            zip(self._textures, ("uPlaneY", "uPlaneU", "uPlaneV"), strict=True)
        ):
            program.setUniformValue(uniform, index)
            self.gl.glActiveTexture(_GL_TEXTURE0 + index)
            texture.bind(index)

        assert self._vao is not None
        self._vao.bind()
        self.gl.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
        self._vao.release()
        program.release()

    # -- internals ----------------------------------------------------------
    def _recreate_textures(self, frame: VideoFrame) -> None:
        for texture in self._textures:
            texture.destroy()
        self._textures = []

        # (width, height) of the Y, U and V planes, in that order.
        shapes = (
            (frame.y.shape[1], frame.y.shape[0]),
            (frame.u.shape[1], frame.u.shape[0]),
            (frame.v.shape[1], frame.v.shape[0]),
        )

        for width, height in shapes:
            texture = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
            texture.setFormat(QOpenGLTexture.TextureFormat.R8_UNorm)
            texture.setSize(int(width), int(height))
            texture.setMipLevels(1)
            texture.allocateStorage()
            texture.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
            self._apply_filter(texture)
            self._textures.append(texture)

        self._uploaded_frame = None

    def _apply_filter(self, texture: QOpenGLTexture) -> None:
        mode = (
            QOpenGLTexture.Filter.Nearest
            if self.filter_mode == "nearest"
            else QOpenGLTexture.Filter.Linear
        )
        texture.setMinificationFilter(mode)
        texture.setMagnificationFilter(mode)
