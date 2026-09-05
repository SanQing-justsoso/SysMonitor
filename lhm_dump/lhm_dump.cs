using System;
using System.Collections.Generic;
using System.Globalization;
using LibreHardwareMonitor.Hardware;

public class UpdateVisitor : IVisitor
{
    public readonly List<IHardware> Hardware = new List<IHardware>();

    public void VisitComputer(IComputer computer) { computer.Traverse(this); }
    public void VisitHardware(IHardware hardware)
    {
        Hardware.Add(hardware);
        hardware.Update();
        foreach (var sh in hardware.SubHardware) sh.Accept(this);
    }
    public void VisitSensor(ISensor sensor) { }
    public void VisitParameter(IParameter parameter) { }
}

public class Program
{
    static string J(string s)
    {
        return s.Replace("\\", "\\\\")
                .Replace("\"", "\\\"")
                .Replace("\r", "\\r")
                .Replace("\n", "\\n")
                .Replace("\t", "\\t");
    }

    static void Emit(IHardware h, List<string> items)
    {
        foreach (var s in h.Sensors)
        {
            if (s.Value.HasValue)
            {
                items.Add(string.Format(
                    "{{\"hw\":\"{0}\",\"htype\":\"{1}\",\"stype\":\"{2}\",\"name\":\"{3}\",\"value\":{4}}}",
                    J(h.Name), h.HardwareType, s.SensorType, J(s.Name),
                    s.Value.Value.ToString("R", CultureInfo.InvariantCulture)));
            }
        }
    }

    public static void Main()
    {
        var computer = new Computer
        {
            IsCpuEnabled = true,
            IsGpuEnabled = true,
            IsMemoryEnabled = true,
            IsMotherboardEnabled = true,
        };
        try
        {
            computer.Open();
            var visitor = new UpdateVisitor();
            computer.Accept(visitor);

            var items = new List<string>();
            foreach (var hw in visitor.Hardware) Emit(hw, items);

            Console.WriteLine("[" + string.Join(",", items) + "]");
        }
        finally
        {
            computer.Close();
        }
    }
}
