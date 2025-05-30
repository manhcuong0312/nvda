# A part of NonVisual Desktop Access (NVDA)
# Copyright (C) 2017-2025 NV Access Limited, James Teh, Leonard de Ruijter, Cyrille Bougot
# This file is covered by the GNU General Public License.
# See the file COPYING for more details.

"""User interface for content recognition.
This module provides functionality to capture an image from the screen
for the current navigator object, pass it to a content recognizer for recognition
and present the result to the user so they can read it with cursor keys, etc.
NVDA scripts or GUI call the L{recognizeNavigatorObject} function with the recognizer they wish to use.
"""

from typing import Optional, Union, TYPE_CHECKING
import api
import ui
import screenBitmap
import NVDAObjects.window
from NVDAObjects.behaviors import LiveText
import controlTypes
import browseMode
import cursorManager
import eventHandler
import textInfos
from logHandler import log
import queueHandler
import core
from scriptHandler import script
from . import RecogImageInfo, ContentRecognizer, RecognitionResult, onRecognizeResultCallbackT

if TYPE_CHECKING:
	import inputCore


class RecogResultNVDAObject(cursorManager.CursorManager, NVDAObjects.window.Window):
	"""Fake NVDAObject used to present a recognition result in a cursor manager.
	This allows the user to read the result with cursor keys, etc.
	Pressing enter will activate (e.g. click) the text at the cursor.
	Pressing escape dismisses the recognition result.
	"""

	role = controlTypes.Role.DOCUMENT
	# Translators: The title of the document used to present the result of content recognition.
	name = _("Result")
	treeInterceptor = None

	def __init__(self, result=None, obj=None):
		self.parent = parent = api.getFocusObject()
		self.result = result
		if result:
			self._selection = self.makeTextInfo(textInfos.POSITION_FIRST)
		super().__init__(windowHandle=parent.windowHandle)

	def makeTextInfo(self, position):
		# Maintain our own fake selection/caret.
		if position == textInfos.POSITION_SELECTION:
			ti = self._selection.copy()
		elif position == textInfos.POSITION_CARET:
			ti = self._selection.copy()
			ti.collapse()
		else:
			ti = self.result.makeTextInfo(self, position)
		return ti

	def setFocus(self):
		ti = self.parent.treeInterceptor
		if isinstance(ti, browseMode.BrowseModeDocumentTreeInterceptor):
			# Normally, when entering browse mode from a descendant (e.g. dialog),
			# we want the cursor to move to the focus (#3145).
			# However, we don't want this for recognition results, as these aren't focusable.
			ti._enteringFromOutside = True
		# This might get called from a background thread and all NVDA events must run in the main thread.
		eventHandler.queueEvent("gainFocus", self)

	def _get_hasFocus(self) -> bool:
		return self is api.getFocusObject()

	def script_activatePosition(self, gesture):
		try:
			self._selection.activate()
		except NotImplementedError:
			log.debugWarning("Result TextInfo does not implement activate")

	# Translators: Describes a command.
	script_activatePosition.__doc__ = _("Activates the text at the cursor if possible")

	def script_exit(self, gesture):
		eventHandler.executeEvent("gainFocus", self.parent)

	# Translators: Describes a command.
	script_exit.__doc__ = _("Dismiss the recognition result")

	# The find commands are tricky to support because they pop up dialogs.
	# This moves the focus, so we lose our fake focus.
	# See https://github.com/nvaccess/nvda/pull/7361#issuecomment-314698991
	def script_find(self, gesture):
		# Translators: Reported when a user tries to use a find command when it isn't supported.
		ui.message(_("Not supported in this document"))

	def script_findNext(self, gesture):
		# Translators: Reported when a user tries to use a find command when it isn't supported.
		ui.message(_("Not supported in this document"))

	def script_findPrevious(self, gesture):
		# Translators: Reported when a user tries to use a find command when it isn't supported.
		ui.message(_("Not supported in this document"))

	__gestures = {
		"kb:enter": "activatePosition",
		"kb:space": "activatePosition",
		"kb:escape": "exit",
	}


class RefreshableRecogResultNVDAObject(RecogResultNVDAObject, LiveText):
	"""NVDA Object that itself is responsible for fetching the recognizition result.
	It is also able to refresh the result at intervals or on demand when the recognizer supports it.
	"""

	def __init__(
		self,
		recognizer: ContentRecognizer,
		imageInfo: RecogImageInfo,
		obj: Optional[NVDAObjects.NVDAObject] = None,
	):
		self.recognizer = recognizer
		self.imageInfo = imageInfo
		super().__init__(result=None, obj=obj)
		LiveText.initOverlayClass(self)

	def _recognize(self, onResult: onRecognizeResultCallbackT):
		if self.result and not self.hasFocus:
			# We've already recognized once, so we did have focus, but we don't any
			# more. This means the user dismissed the recognition result, so we
			# shouldn't recognize again.
			return
		imgInfo = self.imageInfo
		sb = screenBitmap.ScreenBitmap(imgInfo.recogWidth, imgInfo.recogHeight)
		pixels = sb.captureImage(
			imgInfo.screenLeft,
			imgInfo.screenTop,
			imgInfo.screenWidth,
			imgInfo.screenHeight,
		)
		self.recognizer.recognize(pixels, self.imageInfo, onResult)

	def _onFirstResult(self, result: Union[RecognitionResult, Exception]):
		global _activeRecog
		_activeRecog = None
		# This might get called from a background thread, so any UI calls must be queued to the main thread.
		if isinstance(result, Exception):
			log.error(f"Recognition failed: {result}")
			queueHandler.queueFunction(
				queueHandler.eventQueue,
				ui.message,
				# Translators: Reported when recognition (e.g. OCR) fails.
				_("Recognition failed"),
			)
			return
		self.result = result
		self._selection = self.makeTextInfo(textInfos.POSITION_FIRST)
		# This method queues an event to the main thread.
		self.setFocus()
		if self.recognizer.allowAutoRefresh:
			self._scheduleRecognize()

	def _scheduleRecognize(self):
		core.callLater(self.recognizer.autoRefreshInterval, self._recognize, self._onResult)

	@script(
		# Translators: Describes a command.
		description=_("Refresh the recognition result"),
		gesture="kb:NVDA+f5",
	)
	def script_refreshBuffer(self, gesture: "inputCore.InputGesture") -> None:
		if self.recognizer.allowAutoRefresh:
			# Translators: Reported when a manual update of a content recognition result (e.g. OCR result) is
			# requested, but the content is already updated automatically.
			ui.message(_("The result of content recognition is already automatically updated"))
			return
		core.callLater(0, self._recognize, self._onResult)

	def _onResult(self, result: Union[RecognitionResult, Exception]):
		if not self.hasFocus:
			# The user has dismissed the recognition result.
			return
		if isinstance(result, Exception):
			log.error(f"Refresh recognition failed: {result}")
			queueHandler.queueFunction(
				queueHandler.eventQueue,
				ui.message,
				# Translators: Reported when recognition (e.g. OCR) fails during automatic refresh.
				_("Refresh of recognition result failed"),
			)
			self.stopMonitoring()
			return
		self.result = result
		# The current selection refers to the old result. We need to refresh that,
		# but try to keep the same cursor position.
		self.selection = self.makeTextInfo(self._selection.bookmark)
		# Tell LiveText that our text has changed.
		self.event_textChange()
		if self.recognizer.allowAutoRefresh:
			self._scheduleRecognize()

	def event_gainFocus(self):
		super().event_gainFocus()
		if self.recognizer.allowAutoRefresh:
			# Make LiveText watch for and report new text.
			self.startMonitoring()

	def event_loseFocus(self):
		# note: If monitoring has not been started, this will have no effect.
		self.stopMonitoring()
		super().event_loseFocus()

	def start(self):
		self._recognize(self._onFirstResult)


#: Keeps track of the recognition in progress, if any.
_activeRecog = None


def recognizeNavigatorObject(recognizer: ContentRecognizer):
	"""User interface function to recognize content in the navigator object.
	This should be called from a script or in response to a GUI action.
	@param recognizer: The content recognizer to use.
	"""
	global _activeRecog
	if isinstance(api.getFocusObject(), RecogResultNVDAObject):
		# Translators: Reported when content recognition (e.g. OCR) is attempted,
		# but the user is already reading a content recognition result.
		ui.message(_("Already in a content recognition result"))
		return
	nav = api.getNavigatorObject()
	if not recognizer.validateObject(nav):
		return
	# Translators: Reported when content recognition (e.g. OCR) is attempted,
	# but the content is not visible.
	notVisibleMsg = _("Content is not visible")
	try:
		left, top, width, height = nav.location
	except TypeError:
		log.debugWarning("Object returned location %r" % nav.location)
		ui.message(notVisibleMsg)
		return
	if not recognizer.validateCaptureBounds(nav.location):
		return
	try:
		imgInfo = RecogImageInfo.createFromRecognizer(left, top, width, height, recognizer)
	except ValueError:
		ui.message(notVisibleMsg)
		return
	if _activeRecog:
		_activeRecog.recognizer.cancel()
	# Translators: Reporting when content recognition (e.g. OCR) begins.
	ui.message(_("Recognizing"))
	_activeRecog = RefreshableRecogResultNVDAObject(recognizer=recognizer, imageInfo=imgInfo)
	_activeRecog.start()
