# A part of NonVisual Desktop Access (NVDA)
# This file is covered by the GNU General Public License.
# See the file COPYING for more details.
# Copyright (C) 2023-2023 NV Access Limited

from typing import Optional, Self
import contextlib
import _ctypes
import ctypes
from ctypes import (
	_SimpleCData,
	c_long,
	c_ulong,
	c_ushort,
	c_byte,
	c_char,
	c_wchar,
)
from comtypes import GUID
from dataclasses import dataclass
import inspect
import os
import struct
import itertools
from UIAHandler import UIA
from . import lowLevel
from logHandler import getCodePath


def _getLocationString(frame: inspect.FrameInfo) -> str:
	"""
	Returns a string describing the location of the given frame.
	It includes all ancestor frames with the same file path,
	plus one more frame with a different file path,
	so you can see what called into the file.
	"""
	locations = []
	oldPath = None
	while frame:
		path = os.path.relpath(inspect.getfile(frame))
		locations.append(
			f"File \"{path}\", line {frame.f_lineno}, in {frame.f_code.co_name}"
		)
		if oldPath and path != oldPath:
			break
		oldPath = path
		frame = frame.f_back
	locationString = "\n".join(reversed(locations))
	return locationString


@dataclass
class _InstructionRecord:
	instructionType: lowLevel.InstructionType
	params: list[bytes]
	locationString: str

	def __repr__(self):
		return f"{self.instructionType.name}({', '.join(map(repr, self.params))})\n{self.locationString}"


class _RemoteBaseObject:
	""" A base class for all remote objects. """

	_isTypeInstruction: lowLevel.InstructionType

	@classmethod
	def _new(cls, rob: "RemoteOperationBuilder", initialValue: object=None) -> "_RemoteBaseObject":
		operandId = rob._getNewOperandId()
		cls._initOperand(rob, operandId, initialValue)
		return cls(rob, operandId)

	@classmethod
	def _initOperand(cls, operandId: int, initialValue: object):
		raise NotImplementedError()

	def __init__(self, rob: "RemoteOperationBuilder", operandId: int):
		self._rob = rob
		self._operandId = operandId

	def stringify(self) -> "RemoteString":
		resultOperandId = self._rob._getNewOperandId() 
		result = RemoteString(self._rob, resultOperandId)
		self._rob._addInstruction(
			lowLevel.InstructionType.Stringify,
			c_long(resultOperandId),
			c_long(self._operandId)
		)
		return result


class _RemoteIntegral(_RemoteBaseObject):
	_newInstruction: lowLevel.InstructionType
	_initialValueType = _SimpleCData

	@classmethod
	def _initOperand(cls, rob: "RemoteOperationBuilder", operandId: int, initialValue: object):
		rob._addInstruction(
			cls._newInstruction,
			c_long(operandId),
			cls._initialValueType(initialValue)
		)


class RemoteInt(_RemoteIntegral):
	_isTypeInstruction = lowLevel.InstructionType.IsInt
	_newInstruction = lowLevel.InstructionType.NewInt
	_initialValueType = c_long


class RemoteBool(_RemoteIntegral):
	_isTypeInstruction = lowLevel.InstructionType.IsBool
	_newInstruction = lowLevel.InstructionType.NewBool
	_initialValueType = c_byte


class RemoteString(_RemoteBaseObject):
	_isTypeInstruction = lowLevel.InstructionType.IsString

	@classmethod
	def _initOperand(cls, rob: "RemoteOperationBuilder", operandId: int, initialValue: str):
		rob._addInstruction(
			lowLevel.InstructionType.NewString,
			c_long(operandId),
			ctypes.create_unicode_buffer(initialValue)
		)

	def _concat(self, other, toResult) -> None:
		if not isinstance(toResult, RemoteString):
			raise TypeError("toResult must be a RemoteString")
		if not isinstance(other, RemoteString):
			if isinstance(other, str):
				other = self._rob.newString(other)
			elif isinstance(other, _RemoteBaseObject):
				other = other.stringify()
			else:
				raise TypeError("other must be a RemoteString, a str, or a _RemoteBaseObject")
		self._rob._addInstruction(
			lowLevel.InstructionType.RemoteStringConcat ,
			c_long(toResult._operandId),
			c_long(self._operandId),
			c_long(other._operandId)
		)

	def __add__(self, other: Self | _RemoteBaseObject | str) -> Self:
		resultOperandId = self._rob._getNewOperandId()
		result = RemoteString(self._rob, resultOperandId)
		self._concat(other, result)
		return result

	def __iadd__(self, other: Self | _RemoteBaseObject | str) -> Self:
		self._concat(other, self)
		return self


class _RemoteNullable(_RemoteBaseObject):

	@classmethod
	def _initOperand(cls, rob: "RemoteOperationBuilder", operandId: int, initialValue: None=None):
		rob._addInstruction(
			lowLevel.InstructionType.NewNull,
			c_long(operandId),
		)

	def isNull(self) -> bool:
		result = self._rob.newBool()
		self._rob._addInstruction(
			lowLevel.InstructionType.IsNull,
			c_long(result._operandId),
			c_long(self._operandId)
		)
		return result


class RemoteVariant(_RemoteNullable):

	def isType(self, remoteClass: type[_RemoteBaseObject]) -> bool:
		if not issubclass(remoteClass, _RemoteBaseObject):
			raise TypeError("remoteClass must be a subclass of _RemoteBaseObject")
		result = self._rob.newBool()
		self._rob._addInstruction(
			lowLevel.InstructionType.IsType,
			c_long(result._operandId),
			c_long(self._operandId)
		)
		return result

	def asType(self, remoteClass: type[_RemoteBaseObject]) -> _RemoteBaseObject:
		return remoteClass(self._rob, self._operandId)


class RemoteExtensionTarget(_RemoteNullable):

	def isExtensionSupported(self, extensionGuid: GUID) -> bool:
		if not isinstance(extensionGuid, RemoteGuid):
			extensionGuid = self._rob.newGuid(extensionGuid)
		resultOperandId = self._rob._getNewOperandId()
		result = RemoteBool(self._rob, resultOperandId)
		self._rob._addInstruction(
			lowLevel.InstructionType.IsExtensionSupported,
			c_long(result._operandId),
			c_long(self._operandId),
			c_ulong(extensionGuid._operandId)
		)
		return result

	def callExtension(self, extensionGuid: GUID, *params: _RemoteBaseObject) -> None:
		if not isinstance(extensionGuid, RemoteGuid):
			extensionGuid = self._rob.newGuid(extensionGuid)
		self._rob._addInstruction(
			lowLevel.InstructionType.CallExtension,
			c_long(self._operandId),
			c_ulong(extensionGuid._operandId),
			c_long(len(params)),
			*(c_long(p._operandId) for p in params)
		)


class RemoteElement(RemoteExtensionTarget):
	_isTypeInstruction = lowLevel.InstructionType.IsElement

	@classmethod
	def _initOperand(cls, rob: "RemoteOperationBuilder", operandId: int, initialValue: UIA.IUIAutomationElement):
		if initialValue is None:
			return super()._initOperand(rob, operandId)
		rob._importElement(operandId, initialValue)

	def getPropertyValue(self, propertyId: int, ignoreDefault: bool=False) -> object:
		if not isinstance(propertyId, RemoteInt):
			propertyId = self._rob.newInt(propertyId)
		if not isinstance(ignoreDefault, RemoteBool):
			ignoreDefault = self._rob.newBool(ignoreDefault)
		resultOperandId = self._rob._getNewOperandId()
		result = RemoteVariant(self._rob, resultOperandId)
		self._rob._addInstruction(
			lowLevel.InstructionType.GetPropertyValue,
			c_long(result._operandId),
			c_long(self._operandId),
			c_long(propertyId._operandId),
			c_long(ignoreDefault._operandId)
		)
		return result


class RemoteTextRange(RemoteExtensionTarget):

	@classmethod
	def _initOperand(cls, rob: "RemoteOperationBuilder", operandId: int, initialValue: UIA.IUIAutomationTextRange):
		if initialValue is None:
			return super()._initOperand(rob, operandId)
		rob._importTextRange(operandId, initialValue)


class RemoteGuid(_RemoteBaseObject):
	_isTypeInstruction = lowLevel.InstructionType.IsGuid

	@classmethod
	def _initOperand(cls, rob: "RemoteOperationBuilder", operandId: int, initialValue: GUID):
		rob._addInstruction(
			lowLevel.InstructionType.NewGuid,
			c_long(operandId),
			c_ulong(initialValue.Data1),
			c_ushort(initialValue.Data2),
			c_ushort(initialValue.Data3),
			*(c_byte(b) for b in initialValue.Data4)
		)


class MalformedBytecodeException(RuntimeError):
	pass


class InstructionLimitExceededException(RuntimeError):
	pass


class RemoteException(RuntimeError):
	pass


class ExecutionFailureException(RuntimeError):
	pass

class UnsupportedOpcodeException(RuntimeError):
	pass

class RemoteOperationBuilder:

	_versionBytes = struct.pack('l', 0) 

	def __init__(self, enableLogging=False):
		self._instructions: list[_InstructionRecord] = []
		self._operandIdGen = itertools.count(start=1)
		self._ro = lowLevel.RemoteOperation()
		self._scopeStack: list["_RemoteScopeContext"] = []
		self._results = None
		self._supportedOpcodesCache = set()
		self._loggingEnablede = enableLogging
		if enableLogging:
			self._log: RemoteString = self.newString()
			self.addToResults(self._log)

	def _isOpcodeSupported(self, opcode: lowLevel.InstructionType) -> bool:
		if opcode in self._supportedOpcodesCache:
			return True
		result = True #self._ro.isOpcodeSupported(opcode)
		if result:
			self._supportedOpcodesCache.add(opcode)
		return result

	def _getNewOperandId(self) -> int:
		return next(self._operandIdGen)

	def _addInstruction(self, instruction: lowLevel.InstructionType, *params: _SimpleCData):
		""" Adds an instruction to the instruction list and returns the index of the instruction. """
		""" Adds an instruction to the instruction list and returns the index of the instruction. """
		if not self._isOpcodeSupported(instruction):
			raise UnsupportedOpcodeException(f"Opcode {instruction.name} is not supported")
		frame = inspect.currentframe().f_back
		locationString = _getLocationString(frame)
		self._instructions.append(
			_InstructionRecord(instruction, params, locationString)
		)
		return len(self._instructions) - 1

	def _generateByteCode(self) -> bytes:
		byteCode = b''
		for instruction in self._instructions:
			byteCode += struct.pack('l', instruction.instructionType)
			for param in instruction.params:
				paramBytes = (c_char*ctypes.sizeof(param)).from_address(ctypes.addressof(param)).raw
				if isinstance(param, _ctypes.Array) and param._type_ == c_wchar:
					paramBytes = paramBytes[:-2]
					byteCode += struct.pack('l', len(param) - 1)
				byteCode += paramBytes
		return byteCode

	def _importElement(self, operandId: int, element: UIA.IUIAutomationElement):
		self._ro.importElement(operandId, element)

	def _importTextRange(self, operandId: int, textRange: UIA.IUIAutomationTextRange):
		self._ro.importTextRange(operandId, textRange)

	def newInt(self, initialValue: int=0) -> RemoteInt:
		return RemoteInt._new(self, initialValue)

	def newBool(self, initialValue: bool=False) -> RemoteBool:
		return RemoteBool._new(self, initialValue)

	def newString(self, initialValue: str="") -> RemoteString:
		return RemoteString._new(self, initialValue)

	def newVariant(self) -> RemoteVariant:
		return RemoteVariant._new(self)

	def newExtensionTarget(self) -> RemoteExtensionTarget:
		return RemoteExtensionTarget._new(self)

	def newElement(self, initialValue: UIA.IUIAutomationElement) -> RemoteElement:
		return RemoteElement._new(self, initialValue)

	def newTextRange(self, initialValue: UIA.IUIAutomationTextRange) -> RemoteTextRange:
		return RemoteTextRange._new(self, initialValue)

	def newGuid(self, initialValue: GUID) -> RemoteGuid:
		return RemoteGuid._new(self, initialValue)

	@property
	def _lastInstructionIndex(self):
		return len(self._instructions) - 1

	def _getInstructionRecord(self, instructionIndex: int) -> _InstructionRecord:
		return self._instructions[instructionIndex]

	@property
	def _currentScope(self) -> Optional["_RemoteScopeContext"]:
		return self._scopeStack[-1] if self._scopeStack else None

	def IfBlockContext(self, condition: RemoteBool):
		return _RemoteIfBlockBuilder(self, condition)

	def Else(self):
		if not isinstance(self._currentScope, _RemoteIfBlockBuilder):
			raise RuntimeError("Else block called outside of If block")
		self._currentScope.Else()

	def addToResults(self, remoteObj: _RemoteBaseObject):
		self._ro.addToResults(remoteObj._operandId)

	def halt(self):
		self._addInstruction(lowLevel.InstructionType.Halt)

	def logMessage(self,*strings): 
		if not self._loggingEnablede:
			return
		for string in strings:
			self._log += string
		self._log += "\n"

	def execute(self):
		self.halt()
		byteCode = self._generateByteCode()
		self._results = self._ro.execute(self._versionBytes + byteCode)
		status = self._results.status
		if status == lowLevel.RemoteOperationStatus.MalformedBytecode:
			raise MalformedBytecodeException()
		elif status == lowLevel.RemoteOperationStatus.InstructionLimitExceeded:
			raise InstructionLimitExceededException()
		elif status == lowLevel.RemoteOperationStatus.UnhandledException:
			instructionRecord = self._getInstructionRecord(self._results.errorLocation)
			message = f"\nError at instruction {self._results.errorLocation}: {instructionRecord}\nExtended error: {self._results.extendedError}"
			if self._loggingEnablede:
				try:
					logText = self.dumpLog()
					message += f"\n{logText}"
				except Exception as e:
					message += f"\nFailed to dump log: {e}\n"
			message += self._dumpInstructions()
			raise RemoteException(message)
		elif status == lowLevel.RemoteOperationStatus.ExecutionFailure:
			raise ExecutionFailureException()

	def getResult(self, remoteObj: _RemoteBaseObject) -> object:
		if not self._results:
			raise RuntimeError("Not executed")
		operandId = remoteObj._operandId
		if not self._results.hasOperand(operandId):
			raise LookupError("No such operand")
		return self._results.getOperand(operandId).value

	def dumpLog(self):
		if not self._loggingEnablede:
			raise RuntimeError("Logging not enabled")
		if self._log is None:
			return "Empty remote log"
		output = "--- remote log start ---\n"
		output += self.getResult(self._log)
		output += "--- remote log end ---"
		return output

	def _dumpInstructions(self) -> str:
		output = "--- Instructions start ---\n"
		for index, instruction in enumerate(self._instructions):
			output += f"{index}: {instruction.instructionType.name} {instruction.params}\n"
		output += "--- Instructions end ---"
		return output

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_val, exc_tb):
			if exc_type is None:
				self.execute()


class _RemoteScopeContext:

	def __init__(self, remoteOpBuilder: RemoteOperationBuilder):
		self._rob = remoteOpBuilder

	def __enter__(self):
		self._rob._scopeStack.append(self)

	def __exit__(self, exc_type, exc_val, exc_tb):
		self._rob._scopeStack.pop()


class _RemoteIfBlockBuilder(_RemoteScopeContext):

	def __init__(self, remoteOpBuilder: RemoteOperationBuilder, condition: RemoteBool):
		super().__init__(remoteOpBuilder)
		self._condition = condition

	def __enter__(self):
		self._conditionInstructionIndex = self._rob._addInstruction(
			lowLevel.InstructionType.ForkIfFalse ,
			c_long(self._condition._operandId),
			c_long(1), # offset updated in Else method 
		)
		self._inElse = False
		super().__enter__()

	def Else(self):
		if self._inElse:
			raise RuntimeError("Else block already called")
		self._inElse = True
		self._jumpToEndInstructionIndex = self._rob._addInstruction(
			lowLevel.InstructionType.Fork ,
			c_long(1), # offset updated in __exit__ method 
		)
		nextInstructionIndex = self._rob._lastInstructionIndex + 1
		relativeJumpOffset = nextInstructionIndex - self._conditionInstructionIndex
		conditionInstruction = self._rob._getInstructionRecord(self._conditionInstructionIndex)
		conditionInstruction.params[1].value = relativeJumpOffset

	def __exit__(self, exc_type, exc_val, exc_tb):
		super().__exit__(exc_type, exc_val, exc_tb)
		if not self._inElse:
			self.Else()
		nextInstructionIndex = self._rob._lastInstructionIndex + 1
		relativeJumpOffset = nextInstructionIndex - self._jumpToEndInstructionIndex
		jumpToEndInstruction = self._rob._getInstructionRecord(self._jumpToEndInstructionIndex)
		jumpToEndInstruction.params[0].value = relativeJumpOffset
