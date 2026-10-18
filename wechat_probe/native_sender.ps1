param([Parameter(Mandatory=$true)][string]$RequestPath,[ValidateSet('stage','invoke')][string]$Phase)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes,WindowsBase
$source=@'
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Automation;
using System.Windows;
public sealed class BoundChatSender {
 [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr hwnd);
 [DllImport("user32.dll")] static extern bool SetCursorPos(int x,int y);
 [StructLayout(LayoutKind.Sequential)] struct Mouse { public int x,y; public uint data,flags,time; public UIntPtr extra; }
 [StructLayout(LayoutKind.Explicit)] struct Union { [FieldOffset(0)] public Mouse mouse; }
 [StructLayout(LayoutKind.Sequential)] struct Input { public uint type; public Union data; }
 [DllImport("user32.dll",SetLastError=true)] static extern uint SendInput(uint n,Input[] input,int size);
 static Condition C(AutomationProperty p,object v){return new PropertyCondition(p,v);}
 public static double Now(){return (DateTime.UtcNow-new DateTime(1970,1,1)).TotalSeconds;}
 public static long Run(string phase,int pid,long started,string text,double until,long expectedHwnd,string attemptPath,string action,bool allowExisting){
  if(action!="invoke"&&action!="click")throw new InvalidOperationException("Unknown send action.");
  if(String.IsNullOrWhiteSpace(text)||text.Length>800||text.IndexOfAny(new char[]{'\r','\n','\t','\0'})>=0)throw new InvalidOperationException("Invalid approved text.");
  using(Process p=Process.GetProcessById(pid))using(Process me=Process.GetCurrentProcess()){
   long at=(p.StartTime.ToUniversalTime().Ticks-new DateTime(1970,1,1,0,0,0,DateTimeKind.Utc).Ticks)/10000;
   if(p.HasExited||p.SessionId!=me.SessionId||Math.Abs(at-started)>2)throw new InvalidOperationException("Bound process changed.");
   IntPtr hwnd=p.MainWindowHandle;
   if(hwnd==IntPtr.Zero||(expectedHwnd!=0&&hwnd.ToInt64()!=expectedHwnd)||(phase=="invoke"&&GetForegroundWindow()!=hwnd))throw new InvalidOperationException("Bound chat is not foreground.");
   var root=AutomationElement.FromHandle(hwnd);
   if(root.Current.ProcessId!=pid||root.Current.ClassName!="mmui::MainWindow")throw new InvalidOperationException("Unexpected main window.");
   var pages=root.FindAll(TreeScope.Descendants,new AndCondition(C(AutomationElement.AutomationIdProperty,"chat_message_page"),C(AutomationElement.ClassNameProperty,"mmui::ChatMessagePage")));
   if(pages.Count!=1)throw new InvalidOperationException("Chat page is ambiguous.");
   var page=pages[0];
   var headers=page.FindAll(TreeScope.Descendants,C(AutomationElement.ClassNameProperty,"mmui::ChatTitleBarChatRoomView"));
   if(headers.Count!=1||headers[0].FindAll(TreeScope.Descendants,new AndCondition(C(AutomationElement.ClassNameProperty,"mmui::XTextView"),C(AutomationElement.NameProperty,"test-group"))).Count!=1)throw new InvalidOperationException("Group title changed.");
   var inputs=page.FindAll(TreeScope.Descendants,new AndCondition(C(AutomationElement.AutomationIdProperty,"chat_input_field"),C(AutomationElement.ClassNameProperty,"mmui::ChatInputField"),C(AutomationElement.ControlTypeProperty,ControlType.Edit)));
   if(inputs.Count!=1)throw new InvalidOperationException("Chat input is ambiguous.");
   var input=inputs[0];
   if(input.Current.ProcessId!=pid||input.Current.Name!="test-group"||input.Current.IsOffscreen||!input.Current.IsEnabled||(phase=="invoke"&&!input.Current.HasKeyboardFocus))throw new InvalidOperationException("Chat input changed.");
   object vo,to;
   if(!input.TryGetCurrentPattern(ValuePattern.Pattern,out vo)||!input.TryGetCurrentPattern(TextPattern.Pattern,out to))throw new InvalidOperationException("Input patterns unavailable.");
   var value=(ValuePattern)vo;var pattern=(TextPattern)to;
   if(value.Current.IsReadOnly)throw new InvalidOperationException("Input is read-only.");
   var buttons=page.FindAll(TreeScope.Descendants,new AndCondition(C(AutomationElement.ClassNameProperty,"mmui::XOutlineButton"),C(AutomationElement.NameProperty,"\u53d1\u9001"),C(AutomationElement.ControlTypeProperty,ControlType.Button)));
   if(buttons.Count!=1||buttons[0].Current.ProcessId!=pid||buttons[0].Current.IsOffscreen)throw new InvalidOperationException("Send button is ambiguous.");
   object invokeObject;
   if(!buttons[0].TryGetCurrentPattern(InvokePattern.Pattern,out invokeObject))throw new InvalidOperationException("Send invocation unavailable.");
   if(Now()>=until)throw new InvalidOperationException("Command expired.");
   if(phase=="stage"){
    bool empty=value.Current.Value==""&&pattern.DocumentRange.GetText(801)=="";
    if(!empty&&!(allowExisting&&value.Current.Value==text&&pattern.DocumentRange.GetText(801)==text))throw new InvalidOperationException("Existing input must be preserved.");
    // Focus only after the current native group title/input have been checked.
    // Windows may refuse foreground activation; do not bypass that refusal.
    if(GetForegroundWindow()!=hwnd&&!SetForegroundWindow(hwnd))throw new InvalidOperationException("Bound chat is not foreground.");
    if(!input.Current.HasKeyboardFocus)input.SetFocus();
    if(GetForegroundWindow()!=hwnd||!input.Current.HasKeyboardFocus)throw new InvalidOperationException("Bound chat is not foreground.");
    if(empty)value.SetValue(text);
    for(int i=0;i<20&&!buttons[0].Current.IsEnabled;i++)Thread.Sleep(50);
   }
   if(value.Current.Value!=text||pattern.DocumentRange.GetText(801)!=text||!buttons[0].Current.IsEnabled||!input.Current.HasKeyboardFocus||GetForegroundWindow()!=hwnd||Now()>=until)throw new InvalidOperationException("Approved input or focus differs.");
   if(phase=="invoke"){
    Point point=new Point();
    if(action=="click"){
     if(!buttons[0].TryGetClickablePoint(out point))throw new InvalidOperationException("Send point unavailable.");
     var hit=AutomationElement.FromPoint(point);bool matches=false;
     for(int depth=0;depth<8&&hit!=null;depth++,hit=TreeWalker.RawViewWalker.GetParent(hit)){
      if(Automation.Compare(hit,buttons[0])){matches=true;break;}
     }
     if(!matches||GetForegroundWindow()!=hwnd||value.Current.Value!=text||Now()>=until)throw new InvalidOperationException("Send point no longer belongs to the bound button.");
    }
    // Persist before the one desktop action. Never retry Invoke or substitute Enter.
    using(var f=new FileStream(attemptPath,FileMode.CreateNew,FileAccess.Write,FileShare.None)){
     byte[] data=Encoding.UTF8.GetBytes("{\"invoke_attempted\":true}");f.Write(data,0,data.Length);f.Flush(true);
    }
    if(action=="invoke")((InvokePattern)invokeObject).Invoke();
    else{
     if(!SetCursorPos((int)Math.Round(point.X),(int)Math.Round(point.Y)))throw new InvalidOperationException("Send cursor positioning failed.");
     var down=new Input();down.data.mouse.flags=0x0002;
     var up=new Input();up.data.mouse.flags=0x0004;
     uint inserted=SendInput(2,new Input[]{down,up},Marshal.SizeOf(typeof(Input)));
     if(inserted==1)SendInput(1,new Input[]{up},Marshal.SizeOf(typeof(Input)));
     if(inserted!=2)throw new InvalidOperationException("Single click outcome unknown.");
    }
   }else if(phase!="stage")throw new InvalidOperationException("Unknown phase.");
   return hwnd.ToInt64();
  }
 }
}
'@
Add-Type -TypeDefinition $source -ReferencedAssemblies UIAutomationClient,UIAutomationTypes,WindowsBase
$request=Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8|ConvertFrom-Json
$hwnd=[BoundChatSender]::Run($Phase,$request.process.pid,[long]($request.process.started*1000),$request.command.text,$request.command.execute_before,[long]$request.hwnd,(Join-Path (Split-Path $RequestPath) 'native-attempt.json'),$request.native_action,[bool]$request.allow_existing)
if($Phase -eq 'stage'){
 [ordered]@{source='native-uia';observed_at=[BoundChatSender]::Now();process=$request.process;hwnd=$hwnd;chat_type='group';title='test-group';input_text=$request.command.text;foreground=$true;input_focused=$true}|ConvertTo-Json -Depth 5 -Compress
}else{
 [ordered]@{action_returned=$true;native_action=$request.native_action;input_or_send_performed=$true}|ConvertTo-Json -Compress
}
